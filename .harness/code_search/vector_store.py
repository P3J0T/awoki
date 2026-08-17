from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import rag_backend


QDRANT_RETRIEVE_BATCH_SIZE = 256
QDRANT_WRITE_BATCH_SIZE = 128


def _embedding_failure_kind(exc: BaseException) -> str:
    """Classify embedding failures for bounded retry/split policy.

    This classification is deliberately conservative: only transport-like failures
    are retryable, and only timeout/request-capacity failures may trigger adaptive
    request splitting. Auth/schema/protocol failures fail immediately.
    """
    status_code = getattr(exc, "status_code", None)
    try:
        code = int(status_code) if status_code is not None else 0
    except Exception:
        code = 0
    name = type(exc).__name__.lower()
    message = str(exc).lower()

    if code == 413 or "request entity too large" in message or "payload too large" in message:
        return "capacity"
    if code == 408 or "timeout" in name or "timed out" in message or "timeout" in message:
        return "timeout"
    if code == 429 or "rate limit" in message or "too many requests" in message:
        return "rate_limit"
    if code in {409, 425} or 500 <= code <= 599:
        return "server_transient"
    if any(token in name or token in message for token in (
        "connection reset", "connection error", "connectionerror",
        "temporarily unavailable", "service unavailable", "broken pipe",
        "remote protocol error",
    )):
        return "connection_transient"
    if code in {400, 401, 403, 404, 422}:
        return "permanent"
    if any(token in message for token in (
        "unauthorized", "forbidden", "invalid api key", "authentication",
        "dimension", "malformed", "unsupported embedding provider",
    )):
        return "permanent"
    return "unknown"


def _is_transient_embedding_error(exc: BaseException) -> bool:
    return _embedding_failure_kind(exc) in {
        "capacity", "timeout", "rate_limit", "server_transient", "connection_transient",
    }


def _can_adapt_embedding_batch(exc: BaseException) -> bool:
    return _embedding_failure_kind(exc) in {"capacity", "timeout"}


def _batches(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _collection_exists(client: Any, collection: str) -> bool:
    try:
        return bool(client.collection_exists(collection))
    except AttributeError:
        try:
            client.get_collection(collection)
            return True
        except Exception as exc:
            # Older clients do not expose collection_exists. Only a missing
            # collection should be converted to False; transport/auth failures
            # must abort the membership transaction instead of risking payload
            # replacement from an incomplete read.
            name = type(exc).__name__.lower()
            message = str(exc).lower()
            if "not found" in message or "doesn't exist" in message or "404" in message or "notfound" in name:
                return False
            raise


def embedding_identity() -> dict[str, Any]:
    """Return only settings that can change document vector semantics.

    Operational details such as batch size or whether authentication happens to
    be configured must not invalidate reusable vectors. Prefix contents do
    matter, so they are included directly rather than represented as booleans.
    """
    profile = rag_backend.embedding_profile()
    config = rag_backend.embedding_config()
    return {
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "deployment_id": profile.get("deployment_id"),
        "base_url": profile.get("base_url"),
        "normalize": bool(profile.get("normalize")),
        "document_prefix": config.document_prefix,
        "explicit_vector_size": profile.get("explicit_vector_size"),
    }


def embedding_profile_hash() -> str:
    normalized = json.dumps(embedding_identity(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def code_collection_name() -> str:
    explicit = os.environ.get("AWOKI_CODE_QDRANT_COLLECTION", "").strip()
    if explicit:
        return explicit
    return f"{rag_backend.qdrant_collection_name()}_code_v1"


def _point_id(embedding_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"awoki-code:{embedding_key}"))



def _point_struct(*, point_id: str, vector: list[float], payload: dict[str, Any]) -> Any:
    """Build a Qdrant point without making model imports part of core logic.

    Production images include qdrant-client. The lightweight fallback keeps the
    deterministic membership algorithm independently testable with a fake
    client and is never used by a real qdrant-client installation.
    """
    try:
        from qdrant_client.models import PointStruct

        return PointStruct(id=point_id, vector=vector, payload=payload)
    except ModuleNotFoundError:
        return SimpleNamespace(id=point_id, vector=vector, payload=payload)


def _point_ids_selector(point_ids: list[str]) -> Any:
    try:
        from qdrant_client.models import PointIdsList

        return PointIdsList(points=point_ids)
    except ModuleNotFoundError:
        return SimpleNamespace(points=point_ids)


def _membership_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(item.get("project_id") or ""),
        str(item.get("source_id") or item.get("repo_id") or ""),
        str(item.get("branch_key") or ""),
        str(item.get("path") or ""),
        str(item.get("chunk_id") or ""),
    )


def _scope_key(project_id: str, branch_key: str) -> str:
    return f"{project_id}\x1f{branch_key}"


def _payload_memberships(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("memberships")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_payload(embedding_key: str, content_hash: str, memberships: list[dict[str, Any]]) -> dict[str, Any]:
    memberships = sorted(memberships, key=_membership_key)
    return {
        "point_kind": "code_chunk",
        "embedding_key": embedding_key,
        "content_hash": content_hash,
        "project_ids": sorted({str(row.get("project_id") or "") for row in memberships if row.get("project_id")}),
        "repo_ids": sorted({str(row.get("repo_id") or "") for row in memberships if row.get("repo_id")}),
        "source_ids": sorted({str(row.get("source_id") or "") for row in memberships if row.get("source_id")}),
        "source_types": sorted({str(row.get("source_type") or "") for row in memberships if row.get("source_type")}),
        "revision_keys": sorted({str(row.get("revision_key") or "") for row in memberships if row.get("revision_key")}),
        "branch_keys": sorted({str(row.get("branch_key") or "") for row in memberships if row.get("branch_key")}),
        # A combined scope key prevents false Qdrant pre-filter matches caused
        # by independently matching a project from one membership and a branch
        # from another membership on the same content-addressed point.
        "scope_keys": sorted({
            _scope_key(str(row.get("project_id") or ""), str(row.get("branch_key") or ""))
            for row in memberships
            if row.get("project_id") and row.get("branch_key")
        }),
        "memberships": memberships,
        # Persist semantic identity only. Operational knobs such as timeout,
        # retries, and batch size must not churn thousands of otherwise-current
        # Qdrant payloads when operators tune throughput/reliability.
        "embedding": embedding_identity(),
    }


def _payload_materially_equal(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """Compare only fields that affect membership/search correctness."""
    scalar_fields = ("point_kind", "embedding_key", "content_hash")
    if any(existing.get(field) != desired.get(field) for field in scalar_fields):
        return False
    list_fields = ("project_ids", "repo_ids", "source_ids", "source_types", "revision_keys", "branch_keys", "scope_keys")
    for field in list_fields:
        if sorted(existing.get(field) or []) != sorted(desired.get(field) or []):
            return False
    old_memberships = sorted(_payload_memberships(existing), key=_membership_key)
    new_memberships = sorted(_payload_memberships(desired), key=_membership_key)
    return old_memberships == new_memberships


def _membership_from_row(project_id: str, branch_key: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "repo_id": str(row.get("repo_id") or ""),
        "source_id": str(row.get("source_id") or row.get("repo_id") or ""),
        "source_type": str(row.get("source_type") or "git"),
        "revision_key": str(row.get("revision_key") or branch_key),
        "content_identity": str(row.get("content_identity") or row.get("commit_sha") or branch_key),
        "branch_key": branch_key,
        "path": str(row.get("path") or ""),
        "chunk_id": str(row.get("chunk_id") or ""),
        "symbol_id": str(row.get("symbol_id") or ""),
        "symbol_name": str(row.get("symbol_name") or ""),
        "qualified_name": str(row.get("qualified_name") or ""),
        "symbol_kind": str(row.get("symbol_kind") or ""),
        "language": str(row.get("language") or ""),
        "start_line": int(row.get("start_line") or 1),
        "end_line": int(row.get("end_line") or row.get("start_line") or 1),
        "commit_sha": str(row.get("commit_sha") or ""),
        "dirty": bool(row.get("dirty")),
    }


def membership_hash(rows: list[dict[str, Any]], project_id: str, branch_key: str) -> str:
    values = []
    for row in rows:
        membership = _membership_from_row(project_id, branch_key, row)
        values.append(f"{row.get('embedding_key')}|{json.dumps(membership, sort_keys=True, separators=(',', ':'))}")
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _emit_progress(progress_callback: Callable[[dict[str, Any]], None] | None, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(dict(payload))
    except Exception:
        # Progress reporting is observability, not part of the vector
        # transaction. A status-file problem must not corrupt or abort a
        # successful Qdrant materialization.
        return


def sync_branch_memberships(
    *,
    project_id: str,
    branch_key: str,
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Synchronize content-addressed code vectors and branch memberships.

    One Qdrant point is stored per embedding profile + chunk content hash. The
    point may have many project/branch/path memberships. Existing vectors are
    reused without another embedding request.

    ``progress_callback`` receives bounded, non-source telemetry only. It is
    used by the detached refresh worker to expose real batch/chunk progress
    without leaking code text into job-state JSON.
    """
    sync_started = time.monotonic()
    client, status = rag_backend.qdrant_client()
    collection = code_collection_name()
    target_hash = membership_hash(new_rows, project_id, branch_key)
    new_vectors = 0
    reused = 0
    removed = 0
    vectors_reused_content = 0
    vectors_to_embed = 0
    batches_completed = 0
    batches_total = 0
    embedding_worker_retry_attempts = 0
    embedding_adaptive_splits = 0
    chunks_total = len(new_rows)
    chunks_ready = 0
    target_vectors_total = 0
    failure_phase = "initializing"
    inventory_batches = 0
    payload_updates_needed = 0
    if client is None:
        _emit_progress(
            progress_callback,
            phase="failed",
            qdrant_collection=collection,
            reason=str(status),
        )
        return {
            "status": "degraded",
            "backend": "qdrant",
            "collection": collection,
            "reason": status,
            "membership_hash": target_hash,
            "new_vectors": 0,
            "reused_vectors": 0,
            "removed_memberships": 0,
        }
    try:
        failure_phase = "qdrant_inventory"
        _emit_progress(
            progress_callback,
            phase="qdrant_inventory",
            qdrant_collection=collection,
            chunks_total=len(new_rows),
        )
        old_by_key: dict[str, list[dict[str, Any]]] = {}
        new_by_key: dict[str, list[dict[str, Any]]] = {}
        for row in old_rows:
            old_by_key.setdefault(str(row["embedding_key"]), []).append(row)
        for row in new_rows:
            new_by_key.setdefault(str(row["embedding_key"]), []).append(row)
        all_keys = sorted(set(old_by_key) | set(new_by_key))
        point_ids = [_point_id(key) for key in all_keys]
        existing: dict[str, Any] = {}
        collection_exists = _collection_exists(client, collection)
        if point_ids and collection_exists:
            for point_id_batch in _batches(point_ids, QDRANT_RETRIEVE_BATCH_SIZE):
                inventory_batches += 1
                points = client.retrieve(
                    collection_name=collection,
                    ids=point_id_batch,
                    with_payload=True,
                    with_vectors=False,
                )
                existing.update({str(getattr(point, "id", "")): point for point in points})

        pending_new: list[tuple[str, dict[str, Any], str]] = []
        payload_updates: list[tuple[str, dict[str, Any]]] = []
        delete_ids: list[str] = []

        for key in all_keys:
            point_id = _point_id(key)
            point = existing.get(point_id)
            payload = dict(getattr(point, "payload", {}) or {}) if point is not None else {}
            memberships = _payload_memberships(payload)
            current = {_membership_key(item): item for item in memberships}
            for row in old_by_key.get(key, []):
                member = _membership_from_row(project_id, branch_key, row)
                member_key = _membership_key(member)
                if member_key in current and not any(
                    _membership_key(_membership_from_row(project_id, branch_key, candidate)) == member_key
                    for candidate in new_by_key.get(key, [])
                ):
                    current.pop(member_key, None)
                    removed += 1
            for row in new_by_key.get(key, []):
                member = _membership_from_row(project_id, branch_key, row)
                member_key = _membership_key(member)
                if member_key not in current and point is not None:
                    reused += 1
                current[member_key] = member
            merged = list(current.values())
            if not merged:
                if point is not None:
                    delete_ids.append(point_id)
                continue
            content_hash = str((new_by_key.get(key) or old_by_key.get(key) or [{}])[0].get("content_hash") or payload.get("content_hash") or "")
            normalized = _normalize_payload(key, content_hash, merged)
            if point is None:
                representative = (new_by_key.get(key) or old_by_key.get(key) or [{}])[0]
                pending_new.append((key, representative, point_id))
                payload_updates.append((point_id, normalized))
            else:
                if not _payload_materially_equal(payload, normalized):
                    payload_updates.append((point_id, normalized))
                    payload_updates_needed += 1

        target_keys = set(new_by_key)
        existing_target_keys = {
            key for key in target_keys if _point_id(key) in existing
        }
        chunks_total = len(new_rows)
        chunks_ready = sum(len(new_by_key.get(key, [])) for key in existing_target_keys)
        target_vectors_total = len(target_keys)
        vectors_reused_content = len(existing_target_keys)
        vectors_to_embed = len(pending_new)
        embed_batch_size = max(1, int(rag_backend.embedding_profile().get("batch_size") or 32))
        batches_total = (vectors_to_embed + embed_batch_size - 1) // embed_batch_size if vectors_to_embed else 0

        def progress_payload(*, phase: str, vectors_persisted: int = 0, batches_completed: int = 0, reason: str = "") -> dict[str, Any]:
            ready_vectors = vectors_reused_content + vectors_persisted
            percent = 100.0 if chunks_total == 0 else round((chunks_ready / chunks_total) * 100.0, 1)
            return {
                "phase": phase,
                "qdrant_collection": collection,
                "chunks_total": chunks_total,
                "chunks_ready": chunks_ready,
                "target_vectors_total": target_vectors_total,
                "vectors_reused_content": vectors_reused_content,
                "vectors_to_embed": vectors_to_embed,
                "vectors_persisted": vectors_persisted,
                "vectors_ready": ready_vectors,
                "vectors_remaining": max(0, vectors_to_embed - vectors_persisted),
                "batches_total": batches_total,
                "batches_completed": batches_completed,
                "embedding_worker_retry_attempts": embedding_worker_retry_attempts,
                "embedding_adaptive_splits": embedding_adaptive_splits,
                "progress_percent": percent,
                **({"reason": reason} if reason else {}),
            }

        retry_profile = rag_backend.embedding_profile()
        worker_max_retries = max(0, int(retry_profile.get("worker_max_retries") or 0))
        retry_backoff_seconds = max(0.0, float(retry_profile.get("retry_backoff_seconds") or 0.0))
        adaptive_min_batch_size = max(1, int(retry_profile.get("adaptive_min_batch_size") or 1))

        def embed_with_retry(texts: list[str]) -> list[list[float]]:
            nonlocal embedding_worker_retry_attempts
            last_exc: BaseException | None = None
            for attempt in range(worker_max_retries + 1):
                try:
                    return rag_backend.embed_texts(texts, is_query=False)
                except BaseException as exc:
                    last_exc = exc
                    kind = _embedding_failure_kind(exc)
                    if not _is_transient_embedding_error(exc):
                        raise
                    # Bulk timeouts/capacity failures are commonly request-size
                    # sensitive. The provider client has already applied its own
                    # configured transport retry budget, so do not spend the worker
                    # retry budget repeating the same oversized request. Let the
                    # caller split it immediately. Small timeouts can still retry.
                    if kind == "capacity" or (kind == "timeout" and len(texts) > adaptive_min_batch_size):
                        raise
                    if attempt < worker_max_retries:
                        embedding_worker_retry_attempts += 1
                        _emit_progress(
                            progress_callback,
                            **progress_payload(
                                phase="embedding_retry",
                                vectors_persisted=new_vectors,
                                batches_completed=batches_completed,
                                reason=f"transient embedding {kind}; worker retry {attempt + 1}/{worker_max_retries}",
                            ),
                        )
                        if retry_backoff_seconds > 0:
                            time.sleep(min(30.0, retry_backoff_seconds * (2 ** attempt)))
                        continue
                    break
            assert last_exc is not None
            raise last_exc

        if pending_new:
            # Materialize/validate Qdrant *before* the first expensive embedding
            # request. This turns storage/collection failures into an immediate
            # job failure rather than burning CPU on vectors that cannot be
            # persisted.
            failure_phase = "qdrant_preflight"
            _emit_progress(progress_callback, **progress_payload(phase="qdrant_preflight"))
            expected_dim = rag_backend.expected_embedding_dimension()
            rag_backend.ensure_qdrant_collection(client, collection, expected_dim)
            collection_exists = True
            failure_phase = "embedding"
            _emit_progress(progress_callback, **progress_payload(phase="embedding"))

            # Embed and persist incrementally. A first materialization can span
            # thousands of chunks. Each completed batch is committed immediately
            # so a cancelled/interrupted refresh can resume from reusable points.
            payload_map = dict(payload_updates)
            completed_ids: set[str] = set()

            def persist_embedded_batch(batch: list[tuple[str, dict[str, Any], str]], *, reduced: bool = False) -> None:
                nonlocal new_vectors, chunks_ready, embedding_adaptive_splits
                texts = [str(row.get("text") or "") for _, row, _ in batch]
                try:
                    vectors = embed_with_retry(texts)
                except BaseException as exc:
                    if not _can_adapt_embedding_batch(exc) or len(batch) <= adaptive_min_batch_size:
                        raise
                    split = max(adaptive_min_batch_size, len(batch) // 2)
                    if split >= len(batch):
                        raise
                    embedding_adaptive_splits += 1
                    _emit_progress(
                        progress_callback,
                        **progress_payload(
                            phase="embedding_batch_reduce",
                            vectors_persisted=new_vectors,
                            batches_completed=batches_completed,
                            reason=f"transient embedding failure persisted; reducing batch {len(batch)} -> {split}",
                        ),
                    )
                    # Persist successful reduced sub-batches immediately. If a later
                    # sub-batch still fails, completed content-addressed vectors remain
                    # reusable by the next explicit refresh attempt.
                    persist_embedded_batch(batch[:split], reduced=True)
                    persist_embedded_batch(batch[split:], reduced=True)
                    return
                if len(vectors) != len(batch):
                    raise RuntimeError("embedding provider returned an unexpected vector count")
                if not vectors or not vectors[0]:
                    raise RuntimeError("embedding provider returned an empty code vector")
                points = [
                    _point_struct(point_id=point_id, vector=vector, payload=payload_map[point_id])
                    for (_, _, point_id), vector in zip(batch, vectors, strict=True)
                ]
                for point_batch in _batches(points, QDRANT_WRITE_BATCH_SIZE):
                    client.upsert(collection_name=collection, points=point_batch, wait=True)
                new_vectors += len(points)
                batch_keys = [key for key, _, _ in batch]
                chunks_ready += sum(len(new_by_key.get(key, [])) for key in batch_keys)
                completed_ids.update(point_id for _, _, point_id in batch)
                if reduced:
                    _emit_progress(
                        progress_callback,
                        **progress_payload(
                            phase="embedding",
                            vectors_persisted=new_vectors,
                            batches_completed=batches_completed,
                        ),
                    )

            for pending_batch in _batches(pending_new, embed_batch_size):
                persist_embedded_batch(pending_batch)
                batches_completed += 1
                _emit_progress(
                    progress_callback,
                    **progress_payload(
                        phase="embedding",
                        vectors_persisted=new_vectors,
                        batches_completed=batches_completed,
                    ),
                )
            payload_updates = [item for item in payload_updates if item[0] not in completed_ids]
        elif all_keys and not collection_exists:
            # Payload-only reconciliation cannot succeed if the collection was
            # lost. Do not claim the SQLite/Qdrant membership hashes agree.
            raise RuntimeError("code vector collection is missing")

        failure_phase = "finalizing"
        _emit_progress(
            progress_callback,
            **progress_payload(
                phase="finalizing",
                vectors_persisted=new_vectors,
                batches_completed=batches_completed,
            ),
        )
        for point_id, payload in payload_updates:
            client.set_payload(collection_name=collection, payload=payload, points=[point_id], wait=True)
        if delete_ids:
            for delete_batch in _batches(delete_ids, QDRANT_RETRIEVE_BATCH_SIZE):
                client.delete(
                    collection_name=collection,
                    points_selector=_point_ids_selector(delete_batch),
                    wait=True,
                )

        return {
            "status": "indexed",
            "backend": "qdrant",
            "collection": collection,
            "membership_hash": target_hash,
            "new_vectors": new_vectors,
            "reused_vectors": reused,
            "reused_content_vectors": vectors_reused_content,
            "removed_memberships": removed,
            "deleted_points": len(delete_ids),
            "embedding_worker_retry_attempts": embedding_worker_retry_attempts,
            "embedding_adaptive_splits": embedding_adaptive_splits,
            "embedding": rag_backend.embedding_profile(),
            "operations": {
                "inventory_batches": inventory_batches,
                "existing_points": len(existing),
                "payload_updates": payload_updates_needed,
                "delete_points": len(delete_ids),
            },
            "elapsed_ms": int((time.monotonic() - sync_started) * 1000),
        }
    except Exception as exc:
        embedding_failure_kind = _embedding_failure_kind(exc) if failure_phase == "embedding" else ""
        retryable_transport_failure = bool(failure_phase == "embedding" and _is_transient_embedding_error(exc))
        persisted_partial_vectors_reusable = bool(new_vectors or vectors_reused_content)
        _emit_progress(
            progress_callback,
            phase="failed",
            qdrant_collection=collection,
            reason=str(exc),
            embedding_failure_kind=embedding_failure_kind,
            retryable_transport_failure=retryable_transport_failure,
            automatic_retries_exhausted=retryable_transport_failure,
            persisted_partial_vectors_reusable=persisted_partial_vectors_reusable,
            embedding_worker_retry_attempts=embedding_worker_retry_attempts,
            embedding_adaptive_splits=embedding_adaptive_splits,
        )
        return {
            "status": "degraded",
            "backend": "qdrant",
            "collection": collection,
            "reason": str(exc),
            "membership_hash": target_hash,
            "phase": failure_phase,
            "new_vectors": new_vectors,
            "reused_vectors": reused,
            "reused_content_vectors": vectors_reused_content,
            "removed_memberships": removed,
            "target_vectors_total": target_vectors_total,
            "vectors_to_embed": vectors_to_embed,
            "vectors_remaining": max(0, vectors_to_embed - new_vectors),
            "chunks_total": chunks_total,
            "chunks_ready": chunks_ready,
            "batches_total": batches_total,
            "batches_completed": batches_completed,
            "failing_batch": (batches_completed + 1) if failure_phase == "embedding" and batches_completed < batches_total else 0,
            "embedding_batch_size": int(rag_backend.embedding_profile().get("batch_size") or 0),
            "embedding_timeout_seconds": int(rag_backend.embedding_profile().get("timeout_seconds") or 0),
            "embedding_failure_kind": embedding_failure_kind,
            "retryable_transport_failure": retryable_transport_failure,
            "automatic_retries_exhausted": retryable_transport_failure,
            "persisted_partial_vectors_reusable": persisted_partial_vectors_reusable,
            "embedding_worker_retry_attempts": embedding_worker_retry_attempts,
            "embedding_adaptive_splits": embedding_adaptive_splits,
            "embedding": rag_backend.embedding_profile(),
            "operations": {
                "inventory_batches": inventory_batches,
                "existing_points": 0,
                "payload_updates": payload_updates_needed,
            },
            "elapsed_ms": int((time.monotonic() - sync_started) * 1000),
        }


def collection_available(timeout: float = 2.0) -> tuple[bool, str]:
    """Explicitly check that the configured code collection is reachable and exists."""
    client, status = rag_backend.qdrant_client(timeout=timeout, verify=True)
    if client is None:
        return False, status
    collection = code_collection_name()
    try:
        exists = _collection_exists(client, collection)
        return (True, "") if exists else (False, "code vector collection is missing")
    except Exception as exc:
        return False, str(exc)


def search_with_status(
    query: str,
    *,
    project_id: str,
    branch_key: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Search the code collection and preserve degradation diagnostics."""
    try:
        qdrant_timeout = float(os.environ.get("AWOKI_CODE_QDRANT_TIMEOUT_SECONDS", "2"))
    except ValueError:
        qdrant_timeout = 2.0
    qdrant_timeout = max(0.25, min(5.0, qdrant_timeout))
    client, client_status = rag_backend.qdrant_client(timeout=qdrant_timeout)
    collection = code_collection_name()
    if client is None:
        return {
            "status": "degraded",
            "backend": "qdrant",
            "collection": collection,
            "reason": client_status,
            "hits": [],
        }
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        vector = rag_backend.embed_query(query)
        query_filter = Filter(must=[
            FieldCondition(
                key="scope_keys",
                match=MatchValue(value=_scope_key(project_id, branch_key)),
            ),
        ])
        try:
            response = client.query_points(
                collection_name=collection,
                query=vector,
                query_filter=query_filter,
                limit=max(limit, 10),
            )
            points = getattr(response, "points", response)
        except AttributeError:
            points = client.search(
                collection_name=collection,
                query_vector=vector,
                query_filter=query_filter,
                limit=max(limit, 10),
            )
        hits: list[dict[str, Any]] = []
        for point in points:
            payload = dict(getattr(point, "payload", {}) or {})
            score = float(getattr(point, "score", 0.0) or 0.0)
            for membership in _payload_memberships(payload):
                if membership.get("project_id") != project_id or membership.get("branch_key") != branch_key:
                    continue
                hits.append({
                    **membership,
                    "embedding_key": payload.get("embedding_key"),
                    "content_hash": payload.get("content_hash"),
                    "score": score,
                    "retrieval_backend": "code_qdrant",
                })
        hits.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return {
            "status": "ok",
            "backend": "qdrant",
            "collection": collection,
            "reason": "",
            "hits": hits[: max(1, min(limit, 200))],
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "backend": "qdrant",
            "collection": collection,
            "reason": f"{type(exc).__name__}: {exc}",
            "hits": [],
        }


def search(query: str, *, project_id: str, branch_key: str, limit: int = 30) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only hits; engine callers use status."""
    return list(search_with_status(
        query, project_id=project_id, branch_key=branch_key, limit=limit
    ).get("hits") or [])
