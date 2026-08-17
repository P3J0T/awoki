from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}")
DEFAULT_VECTOR_SIZE = 768
DEFAULT_EMBEDDING_MODEL = "text-embeddings-inference"
DEFAULT_RERANK_MODEL = ""
_LAST_EMBEDDING_ERROR = ""
_LAST_RERANK_ERROR = ""
_LAST_QDRANT_PROBE: dict[str, Any] = {
    "status": "not_probed",
    "available": None,
    "checked_at": "",
    "elapsed_ms": 0,
    "reason": "live Qdrant health has not been probed in this process",
}


@dataclass(frozen=True)
class SearchDocument:
    id: str
    scope: str
    kind: str
    title: str
    text: str
    source_path: str
    line: int | None = None
    project_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    batch_size: int
    normalize: bool
    query_prefix: str
    document_prefix: str
    explicit_vector_size: int | None


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall((text or "").lower())


def preview(text: str, max_chars: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def stable_doc_id(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(os.environ.get(name, default))))
    except ValueError:
        return default


def vector_size() -> int:
    """Configured fallback vector size.

    Real providers determine their actual dimensionality from the model output.
    This value is only used by the explicit hash fallback and OpenAI-compatible
    models when no better dimension can be inferred.
    """
    return _env_int("AWOKI_VECTOR_SIZE", DEFAULT_VECTOR_SIZE, 64, 8192)


def embedding_config() -> EmbeddingConfig:
    provider = (os.environ.get("AWOKI_EMBEDDING_PROVIDER") or "openai").strip().lower()
    if provider in {"openai-compatible", "openai_compatible"}:
        provider = "openai"
    model = os.environ.get("AWOKI_EMBEDDING_MODEL")
    if not model:
        if provider == "openai":
            model = DEFAULT_EMBEDDING_MODEL
        elif provider == "hash":
            model = f"hash-{vector_size()}"
        else:
            model = DEFAULT_EMBEDDING_MODEL
    return EmbeddingConfig(
        provider=provider,
        model=model,
        batch_size=_env_int("AWOKI_EMBEDDING_BATCH_SIZE", 32, 1, 256),
        normalize=_env_bool("AWOKI_EMBEDDING_NORMALIZE", True),
        query_prefix=os.environ.get("AWOKI_QUERY_PREFIX", ""),
        document_prefix=os.environ.get("AWOKI_DOCUMENT_PREFIX", ""),
        explicit_vector_size=int(os.environ["AWOKI_VECTOR_SIZE"]) if os.environ.get("AWOKI_VECTOR_SIZE", "").isdigit() else None,
    )


def embedding_profile() -> dict[str, Any]:
    cfg = embedding_config()
    base_url = (
        os.environ.get("AWOKI_EMBEDDING_BASE_URL")
        or os.environ.get("AWOKI_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    api_key_configured = bool(
        os.environ.get("AWOKI_EMBEDDING_API_KEY")
        or os.environ.get("AWOKI_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    return {
        "provider": cfg.provider,
        "model": cfg.model,
        "deployment_id": os.environ.get("AWOKI_EMBEDDING_DEPLOYMENT_ID", "").strip(),
        "base_url": base_url,
        "endpoint_configured": bool(base_url),
        "auth_configured": api_key_configured,
        "configuration_ready": cfg.provider != "openai" or bool(base_url or api_key_configured),
        "batch_size": cfg.batch_size,
        "timeout_seconds": _env_int("AWOKI_EMBEDDING_TIMEOUT_SECONDS", 30, 1, 600),
        "provider_max_retries": _env_int("AWOKI_EMBEDDING_MAX_RETRIES", 1, 0, 10),
        "worker_max_retries": _env_int("AWOKI_EMBEDDING_WORKER_MAX_RETRIES", 2, 0, 10),
        "retry_backoff_seconds": _env_float("AWOKI_EMBEDDING_RETRY_BACKOFF_SECONDS", 1.0, 0.0, 30.0),
        "adaptive_min_batch_size": _env_int("AWOKI_EMBEDDING_ADAPTIVE_MIN_BATCH_SIZE", 4, 1, 512),
        "normalize": cfg.normalize,
        "query_prefix_set": bool(cfg.query_prefix),
        "document_prefix_set": bool(cfg.document_prefix),
        "explicit_vector_size": cfg.explicit_vector_size,
        "hash_fallback_allowed": _env_bool("AWOKI_ALLOW_HASH_EMBEDDINGS", False),
    }


def retrieval_runtime_status() -> dict[str, Any]:
    return {
        "embedding": embedding_profile(),
        "rerank": rerank_profile(),
        "last_embedding_error": _LAST_EMBEDDING_ERROR,
        "last_rerank_error": _LAST_RERANK_ERROR,
        "degraded": bool(_LAST_EMBEDDING_ERROR or _LAST_RERANK_ERROR),
    }


def _model_slug(value: str) -> str:
    value = value.lower().replace("/", "_").replace(":", "_")
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    return value.strip("_")[:64] or "model"


def _provider_collection_default() -> str:
    cfg = embedding_config()
    return f"awoki_{_model_slug(cfg.provider)}_{_model_slug(cfg.model)}"


def _normalize_vector(vec: Sequence[float]) -> list[float]:
    out = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in out))
    if norm:
        return [v / norm for v in out]
    return out


def _hash_embedding(text: str, dim: int | None = None) -> list[float]:
    """Explicit fallback embedding, disabled unless selected/allowed.

    This remains useful for tests, air-gapped bootstraps, and emergency degraded
    operation, but Awoki's default is now a remote OpenAI-compatible embedding endpoint.
    """
    dim = dim or vector_size()
    vec = [0.0] * dim
    counts: dict[str, int] = {}
    for tok in tokenize(text):
        counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return vec
    for tok, count in counts.items():
        digest = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign * (1.0 + math.log(count))
    return _normalize_vector(vec)


# Backward-compatible name for older tests/imports. The hash fallback is no
# longer used by default Qdrant retrieval.
def hash_embedding(text: str, dim: int | None = None) -> list[float]:
    return _hash_embedding(text, dim=dim)


def _openai_dimensions_for_model(model: str) -> int:
    if os.environ.get("AWOKI_VECTOR_SIZE", "").isdigit():
        return vector_size()
    m = model.lower()
    if "3-large" in m:
        return 3072
    if "small" in m or "ada" in m:
        return 1536
    return DEFAULT_VECTOR_SIZE


def expected_embedding_dimension(profile: dict[str, Any] | None = None) -> int:
    """Return the configured/known document-vector dimension without embedding text.

    Code-vector refresh uses this to materialize and validate the Qdrant
    collection before starting expensive document embedding. The optional
    profile keeps this helper easy to use in hermetic tests and avoids requiring
    an embedding request merely to learn the intended dimension.
    """
    profile = dict(profile or embedding_profile())
    explicit = profile.get("explicit_vector_size")
    if explicit:
        return int(explicit)
    if str(profile.get("provider") or "").lower() == "openai":
        return _openai_dimensions_for_model(str(profile.get("model") or DEFAULT_EMBEDDING_MODEL))
    return vector_size()


def _openai_embed_texts(texts: list[str], cfg: EmbeddingConfig, *, is_query: bool = False) -> list[list[float]]:
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("openai package is not installed; install requirements.txt") from exc
    api_key = (
        os.environ.get("AWOKI_EMBEDDING_API_KEY")
        or os.environ.get("AWOKI_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url = (
        os.environ.get("AWOKI_EMBEDDING_BASE_URL")
        or os.environ.get("AWOKI_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    if not api_key and not base_url:
        raise RuntimeError("configure AWOKI_EMBEDDING_API_KEY/OPENAI_API_KEY or an OpenAI-compatible AWOKI_EMBEDDING_BASE_URL")
    # OpenAI-compatible local/private endpoints commonly require a syntactic key
    # even when they do not authenticate it. Never log this value. Interactive
    # query embeddings use a deliberately short timeout/no-retry contract so a
    # degraded semantic backend cannot consume the MCP request deadline. Bulk
    # indexing keeps a larger independently configurable budget.
    api_key = api_key or "awoki-local-endpoint"
    timeout = _env_float(
        "AWOKI_EMBEDDING_QUERY_TIMEOUT_SECONDS" if is_query else "AWOKI_EMBEDDING_TIMEOUT_SECONDS",
        5.0 if is_query else 30.0,
        0.25,
        120.0,
    )
    max_retries = _env_int(
        "AWOKI_EMBEDDING_QUERY_MAX_RETRIES" if is_query else "AWOKI_EMBEDDING_MAX_RETRIES",
        0 if is_query else 1,
        0,
        5,
    )
    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    kwargs: dict[str, Any] = {"model": cfg.model, "input": texts}
    if os.environ.get("AWOKI_VECTOR_SIZE", "").isdigit() and cfg.model.startswith("text-embedding-3"):
        kwargs["dimensions"] = vector_size()
    response = client.embeddings.create(**kwargs)
    vectors = [item.embedding for item in response.data]
    return [_normalize_vector(v) if cfg.normalize else [float(x) for x in v] for v in vectors]


def embed_texts(texts: Sequence[str], *, is_query: bool = False) -> list[list[float]]:
    """Embed text with the configured real provider.

    Default: an OpenAI-compatible remote embedding endpoint. Hash vectors are
    available only when explicitly selected for tests or degraded operation.
    """
    cfg = embedding_config()
    clean = [t or "" for t in texts]
    prefix = cfg.query_prefix if is_query else cfg.document_prefix
    prepared = [prefix + t for t in clean]
    global _LAST_EMBEDDING_ERROR
    try:
        if cfg.provider == "hash":
            vectors = [_hash_embedding(t, cfg.explicit_vector_size or vector_size()) for t in prepared]
        elif cfg.provider == "openai":
            vectors = []
            for i in range(0, len(prepared), cfg.batch_size):
                vectors.extend(_openai_embed_texts(prepared[i : i + cfg.batch_size], cfg, is_query=is_query))
        elif _env_bool("AWOKI_ALLOW_HASH_EMBEDDINGS", False):
            vectors = [_hash_embedding(t, cfg.explicit_vector_size or vector_size()) for t in prepared]
        else:
            raise RuntimeError(f"unsupported embedding provider {cfg.provider!r}; set AWOKI_EMBEDDING_PROVIDER=openai|hash")
        _LAST_EMBEDDING_ERROR = ""
        return vectors
    except Exception as exc:
        _LAST_EMBEDDING_ERROR = str(exc)[:1000]
        raise


def embed_query(query: str) -> list[float]:
    vectors = embed_texts([query], is_query=True)
    if not vectors:
        raise RuntimeError("embedding provider returned no query vector")
    return vectors[0]


def fts_db_path(root: Path, scope: str) -> Path:
    if scope == "global":
        return root / "awoki_global_fts.sqlite"
    return root / ".harness" / "index" / "awoki_project_fts.sqlite"


def init_fts(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                id UNINDEXED,
                scope UNINDEXED,
                kind UNINDEXED,
                project_id UNINDEXED,
                source_path UNINDEXED,
                line UNINDEXED,
                title,
                text,
                metadata_json UNINDEXED,
                tokenize = 'unicode61 tokenchars ''.:/_-'''
            )
            """
        )
        conn.commit()


def rebuild_fts(db_path: Path, docs: Iterable[SearchDocument], scope: str | None = None) -> dict[str, Any]:
    init_fts(db_path)
    docs = list(docs)
    with closing(sqlite3.connect(db_path)) as conn:
        if scope:
            conn.execute("DELETE FROM docs_fts WHERE scope = ?", (scope,))
        else:
            conn.execute("DELETE FROM docs_fts")
        conn.executemany(
            """
            INSERT INTO docs_fts(id, scope, kind, project_id, source_path, line, title, text, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    d.id,
                    d.scope,
                    d.kind,
                    d.project_id,
                    d.source_path,
                    d.line,
                    d.title,
                    d.text,
                    json.dumps(d.metadata, ensure_ascii=False, sort_keys=True),
                )
                for d in docs
            ],
        )
        conn.commit()
    return {"status": "indexed", "backend": "sqlite_fts", "db_path": str(db_path), "scope": scope or "all", "document_count": len(docs)}


def replace_fts_sources(
    db_path: Path,
    docs: Iterable[SearchDocument],
    *,
    source_paths: Iterable[str],
    scope: str = "project",
) -> dict[str, Any]:
    """Atomically replace selected source paths without rebuilding other docs."""
    init_fts(db_path)
    docs = list(docs)
    sources = sorted({str(value) for value in source_paths if str(value)})
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if sources:
            placeholders = ",".join("?" for _ in sources)
            conn.execute(
                f"DELETE FROM docs_fts WHERE scope = ? AND source_path IN ({placeholders})",
                [scope, *sources],
            )
        conn.executemany(
            """
            INSERT INTO docs_fts(id, scope, kind, project_id, source_path, line, title, text, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    d.id, d.scope, d.kind, d.project_id, d.source_path, d.line,
                    d.title, d.text, json.dumps(d.metadata, ensure_ascii=False, sort_keys=True),
                )
                for d in docs
            ],
        )
        conn.commit()
    return {
        "status": "indexed",
        "backend": "sqlite_fts",
        "db_path": str(db_path),
        "scope": scope,
        "document_count": len(docs),
        "replaced_source_count": len(sources),
    }


def fts_document_count(db_path: Path, *, scope: str = "project") -> int:
    if not db_path.exists():
        return 0
    init_fts(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM docs_fts WHERE scope = ?", (scope,)).fetchone()
    return int(row[0] if row else 0)


def fts_document_set_hash(db_path: Path, *, scope: str = "project") -> str:
    if not db_path.exists():
        return hashlib.sha256(b"").hexdigest()
    init_fts(db_path)
    digest = hashlib.sha256()
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, text FROM docs_fts WHERE scope = ? ORDER BY id, text",
            (scope,),
        ).fetchall()
    for doc_id, text in rows:
        digest.update(str(doc_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(text).encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def make_fts_query(query: str) -> str:
    toks = tokenize(query)
    if not toks:
        return ""
    # Prefix matching keeps API names, paths, and partial function names useful.
    # Quote terms so paths/addresses do not become FTS operators.
    return " OR ".join(f'"{t}"*' for t in toks[:24])


def search_fts(
    db_path: Path,
    query: str,
    scope: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    init_fts(db_path)
    limit = max(1, min(int(limit), 50))
    fts_query = make_fts_query(query)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if fts_query:
            where = "docs_fts MATCH ?"
            params: list[Any] = [fts_query]
            if scope:
                where += " AND scope = ?"
                params.append(scope)
            if kind:
                where += " AND kind = ?"
                params.append(kind)
            sql = f"""
                SELECT id, scope, kind, project_id, source_path, line, title, text, metadata_json, rank
                FROM docs_fts
                WHERE {where}
                ORDER BY rank
                LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        else:
            clauses: list[str] = []
            params = []
            if scope:
                clauses.append("scope = ?")
                params.append(scope)
            if kind:
                clauses.append("kind = ?")
                params.append(kind)
            where = " AND ".join(clauses) if clauses else "1 = 1"
            rows = conn.execute(
                f"""
                SELECT id, scope, kind, project_id, source_path, line, title, text, metadata_json, 0.0 AS rank
                FROM docs_fts
                WHERE {where}
                ORDER BY rowid DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
    hits: list[dict[str, Any]] = []
    for r in rows:
        metadata = {}
        try:
            metadata = json.loads(r["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {"metadata_parse_error": True}
        raw_rank = float(r["rank"] or 0.0)
        score = 1.0 / (1.0 + abs(raw_rank))
        hits.append(
            {
                "retrieval_backend": "sqlite_fts",
                "id": r["id"],
                "scope": r["scope"],
                "kind": r["kind"],
                "project_id": r["project_id"],
                "source_path": r["source_path"],
                "line": r["line"],
                "title": r["title"],
                "preview": preview(r["text"]),
                "score": score,
                "metadata": metadata,
            }
        )
    return hits


def qdrant_collection_name(dim: int | None = None) -> str:
    explicit = os.environ.get("AWOKI_QDRANT_COLLECTION") or os.environ.get("QDRANT_COLLECTION")
    if explicit:
        return explicit
    return _provider_collection_default()


def _looks_like_container_runtime() -> bool:
    return os.environ.get("AWOKI_MODE") == "container-opencode" or os.environ.get("HARNESS_MODE") == "container-opencode" or Path("/.dockerenv").exists()


def qdrant_url() -> str:
    # In Docker/OpenCode-SSH mode, localhost points to the OpenCode/MCP container,
    # not the Qdrant service. Prefer the Docker-network URL even if a host-local
    # .env leaked AWOKI_QDRANT_URL=http://127.0.0.1:6333 into the process.
    if _looks_like_container_runtime():
        return (
            os.environ.get("AWOKI_QDRANT_CONTAINER_URL")
            or os.environ.get("AWOKI_QDRANT_URL")
            or os.environ.get("QDRANT_URL")
            or "http://qdrant:6333"
        )
    return (
        os.environ.get("AWOKI_QDRANT_URL")
        or os.environ.get("QDRANT_URL")
        or os.environ.get("AWOKI_QDRANT_HOST_URL")
        or "http://127.0.0.1:6333"
    )


def _record_qdrant_probe(*, available: bool | None, status: str, reason: str, elapsed_ms: int = 0) -> None:
    global _LAST_QDRANT_PROBE
    _LAST_QDRANT_PROBE = {
        "status": status,
        "available": available,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_ms": int(elapsed_ms),
        "reason": reason[:1000],
    }


def qdrant_probe_status() -> dict[str, Any]:
    """Return the last recorded live Qdrant probe without doing network I/O."""
    return dict(_LAST_QDRANT_PROBE)


def qdrant_configuration_status() -> dict[str, Any]:
    """Return passive/local Qdrant configuration and client-library status."""
    disabled = _env_bool("AWOKI_DISABLE_QDRANT", False)
    library_error = ""
    try:
        library_available = importlib.util.find_spec("qdrant_client") is not None
    except (ImportError, AttributeError, ValueError) as exc:  # pragma: no cover
        library_available = False
        library_error = str(exc)[:1000]
    return {
        "disabled": disabled,
        "url": qdrant_url(),
        "client_library_available": library_available,
        "client_library_error": library_error,
        "last_probe": qdrant_probe_status(),
    }


def qdrant_client(timeout: float = 5.0, *, verify: bool = True):
    if _env_bool("AWOKI_DISABLE_QDRANT", False):
        if verify:
            _record_qdrant_probe(available=False, status="disabled", reason="AWOKI_DISABLE_QDRANT=1")
        return None, "AWOKI_DISABLE_QDRANT=1"
    try:
        from qdrant_client import QdrantClient
    except Exception as exc:  # pragma: no cover - depends on optional package
        reason = f"qdrant-client unavailable: {exc}"
        if verify:
            _record_qdrant_probe(available=False, status="client_unavailable", reason=reason)
        return None, reason
    started = time.monotonic()
    try:
        client = QdrantClient(url=qdrant_url(), timeout=timeout)
        if verify:
            client.get_collections()
            _record_qdrant_probe(
                available=True,
                status="ok",
                reason="live Qdrant probe succeeded",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return client, "ok"
    except Exception as exc:  # pragma: no cover - depends on runtime server
        reason = f"qdrant unavailable: {exc}"
        if verify:
            _record_qdrant_probe(
                available=False,
                status="unavailable",
                reason=reason,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return None, reason


def probe_retrieval(
    *,
    probe_qdrant: bool = True,
    probe_embedding: bool = False,
    probe_reranker: bool = False,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Explicitly probe retrieval backends using only fixed synthetic text.

    Status APIs are passive. This explicit diagnostic is allowed to perform
    network I/O and never sends project, source, memory, or user text. The
    timeout is an operation-level budget shared across all requested probes.
    """
    global _LAST_EMBEDDING_ERROR, _LAST_RERANK_ERROR
    budget = max(0.25, min(float(timeout_seconds), 10.0))
    deadline = time.monotonic() + budget

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        return max(0.0, min(budget, remaining))

    def budget_exhausted(name: str) -> None:
        result[name] = {
            "status": "budget_exhausted",
            "reason": "retrieval probe operation budget was exhausted before this backend could be checked",
            "network": False,
        }
        result["budget_exhausted"] = True

    result: dict[str, Any] = {
        "status": "ok",
        "network_probe": True,
        "operation_budget_seconds": budget,
        "budget_exhausted": False,
        "qdrant": {"status": "skipped"},
        "embedding": {"status": "skipped"},
        "rerank": {"status": "skipped"},
    }
    if probe_qdrant:
        remaining = remaining_timeout()
        if remaining <= 0:
            budget_exhausted("qdrant")
        else:
            client, reason = qdrant_client(timeout=max(0.1, remaining), verify=True)
            result["qdrant"] = {
                **qdrant_probe_status(),
                "url": qdrant_url(),
                "available": client is not None,
                "client_status": reason,
            }
    if probe_embedding:
        remaining = remaining_timeout()
        if remaining <= 0:
            budget_exhausted("embedding")
        else:
            cfg = embedding_config()
            started = time.monotonic()
            if cfg.provider == "hash":
                vector = hash_embedding("awoki retrieval health probe", cfg.explicit_vector_size or vector_size())
                result["embedding"] = {
                    "status": "ok",
                    "provider": "hash",
                    "vector_size": len(vector),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "network": False,
                }
            else:
                try:
                    from openai import OpenAI
                    configured_key = (
                        os.environ.get("AWOKI_EMBEDDING_API_KEY")
                        or os.environ.get("AWOKI_OPENAI_API_KEY")
                        or os.environ.get("OPENAI_API_KEY")
                    )
                    base_url = (
                        os.environ.get("AWOKI_EMBEDDING_BASE_URL")
                        or os.environ.get("AWOKI_OPENAI_BASE_URL")
                        or os.environ.get("OPENAI_BASE_URL")
                    )
                    if not configured_key and not base_url:
                        raise RuntimeError(
                            "embedding probe requires an API key or OpenAI-compatible base URL"
                        )
                    api_key = configured_key or "awoki-local-endpoint"
                    kwargs: dict[str, Any] = {
                        "api_key": api_key,
                        "timeout": max(0.1, remaining_timeout()),
                        "max_retries": 0,
                    }
                    if base_url:
                        kwargs["base_url"] = base_url
                    client = OpenAI(**kwargs)
                    response = client.embeddings.create(model=cfg.model, input=["awoki retrieval health probe"])
                    size = len(response.data[0].embedding) if response.data else 0
                    _LAST_EMBEDDING_ERROR = ""
                    result["embedding"] = {
                        "status": "ok",
                        "provider": cfg.provider,
                        "vector_size": size,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "network": True,
                    }
                except Exception as exc:
                    _LAST_EMBEDDING_ERROR = str(exc)[:1000]
                    result["embedding"] = {
                        "status": "degraded",
                        "provider": cfg.provider,
                        "reason": _LAST_EMBEDDING_ERROR,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "network": bool(
                            os.environ.get("AWOKI_EMBEDDING_BASE_URL")
                            or os.environ.get("AWOKI_OPENAI_BASE_URL")
                            or os.environ.get("OPENAI_BASE_URL")
                            or os.environ.get("AWOKI_EMBEDDING_API_KEY")
                            or os.environ.get("AWOKI_OPENAI_API_KEY")
                            or os.environ.get("OPENAI_API_KEY")
                        ),
                    }
    if probe_reranker:
        remaining = remaining_timeout()
        if remaining <= 0:
            budget_exhausted("rerank")
        else:
            profile = rerank_profile()
            if not profile.get("enabled"):
                result["rerank"] = {"status": "disabled", "network": False}
            else:
                started = time.monotonic()
                profile = dict(profile)
                profile["timeout_seconds"] = max(0.1, remaining)
                try:
                    rows = _remote_rerank_scores(
                        "awoki retrieval health probe",
                        ["awoki retrieval health probe document"],
                        profile,
                    )
                    _LAST_RERANK_ERROR = ""
                    result["rerank"] = {
                        "status": "ok",
                        "provider": profile.get("provider"),
                        "result_count": len(rows),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "network": True,
                    }
                except Exception as exc:
                    _LAST_RERANK_ERROR = str(exc)[:1000]
                    result["rerank"] = {
                        "status": "degraded",
                        "provider": profile.get("provider"),
                        "reason": _LAST_RERANK_ERROR,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "network": True,
                    }
    if any(
        isinstance(result.get(name), dict)
        and result[name].get("status") in {"degraded", "unavailable", "budget_exhausted"}
        for name in ("qdrant", "embedding", "rerank")
    ):
        result["status"] = "degraded"
    result["elapsed_ms"] = int((budget - max(0.0, deadline - time.monotonic())) * 1000)
    return result



def retrieval_status_snapshot() -> dict[str, Any]:
    """Return passive retrieval configuration and last-known health only."""
    runtime = retrieval_runtime_status()
    qdrant = qdrant_configuration_status()
    last_probe = qdrant["last_probe"]
    return {
        "embedding": runtime["embedding"],
        "rerank": runtime["rerank"],
        "last_embedding_error": runtime["last_embedding_error"],
        "last_rerank_error": runtime["last_rerank_error"],
        "degraded": runtime["degraded"],
        "qdrant_url": qdrant_url(),
        "qdrant_collection": qdrant_collection_name(),
        "qdrant_client": last_probe.get("status") or "not_probed",
        "qdrant_available": last_probe.get("available"),
        "qdrant_client_library_available": qdrant["client_library_available"],
        "qdrant_client_library_error": qdrant["client_library_error"],
        "qdrant_last_probe": last_probe,
        "network_probe_performed": False,
        "status_contract": "passive; use retrieval_probe for live backend checks",
    }


def _qdrant_filter(
    scope: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    memory_only: bool = False,
):
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    must = []
    if scope:
        must.append(FieldCondition(key="scope", match=MatchValue(value=scope)))
    if project_id:
        must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
    if kind:
        must.append(FieldCondition(key="kind", match=MatchValue(value=kind)))
    if memory_only:
        must.append(FieldCondition(key="metadata.memory_record", match=MatchValue(value=True)))
    return Filter(must=must) if must else None


def clear_qdrant_points(scope: str | None = None, project_id: str | None = None, collection_name: str | None = None) -> dict[str, Any]:
    """Delete stale points for a scope/project before a replacement reindex.

    Qdrant upsert alone is not enough for a memory harness: resolved promotion
    candidates, demoted global memories, deleted notes, or removed artifacts would
    otherwise remain retrievable as stale vector hits. This function keeps vector
    retrieval aligned with the current scoped source of truth.
    """
    client, status = qdrant_client()
    collection = collection_name or qdrant_collection_name()
    if client is None:
        return {"status": "skipped", "backend": "qdrant", "operation": "clear", "reason": status, "collection": collection}
    try:
        from qdrant_client.models import FilterSelector

        try:
            if not client.collection_exists(collection):
                return {"status": "skipped", "backend": "qdrant", "operation": "clear", "reason": "collection_missing", "collection": collection}
        except Exception:
            # Older clients may not expose collection_exists reliably. Try the
            # delete path and let it report a runtime error if the collection is absent.
            pass
        query_filter = _qdrant_filter(scope=scope, project_id=project_id)
        if query_filter is None:
            return {"status": "rejected", "backend": "qdrant", "operation": "clear", "reason": "refusing unscoped qdrant clear"}
        client.delete(collection_name=collection, points_selector=FilterSelector(filter=query_filter), wait=True)
        return {"status": "cleared", "backend": "qdrant", "operation": "clear", "collection": collection, "scope": scope, "project_id": project_id}
    except Exception as exc:  # pragma: no cover - depends on qdrant runtime/client version
        return {"status": "error", "backend": "qdrant", "operation": "clear", "collection": collection, "reason": str(exc)}


def _collection_vector_size(info: Any) -> int | None:
    try:
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            # Named-vector collections are not used by Awoki yet; return first.
            first = next(iter(vectors.values()))
            return int(first.size)
        return int(vectors.size)
    except Exception:
        return None


def ensure_qdrant_collection(client: Any, collection_name: str, dim: int) -> None:
    from qdrant_client.models import Distance, VectorParams

    exists = False
    info = None
    try:
        exists = bool(client.collection_exists(collection_name))
    except Exception:
        try:
            info = client.get_collection(collection_name)
            exists = True
        except Exception:
            exists = False
    if exists and info is None:
        try:
            info = client.get_collection(collection_name)
        except Exception:
            info = None
    if exists:
        existing_dim = _collection_vector_size(info)
        if existing_dim and existing_dim != dim:
            if _env_bool("AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH", False):
                client.delete_collection(collection_name)
                exists = False
            else:
                raise RuntimeError(
                    f"Qdrant collection {collection_name!r} has vector size {existing_dim}, "
                    f"but configured embedding model returns {dim}. Set AWOKI_QDRANT_COLLECTION "
                    "to a new name or AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH=1."
                )
    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


def _doc_embedding_text(d: SearchDocument) -> str:
    # Include structured fields so a query for a function, path, or tag has dense signal.
    tags = d.metadata.get("tags") if isinstance(d.metadata, dict) else None
    if isinstance(tags, list):
        tag_text = " ".join(str(t) for t in tags)
    else:
        tag_text = str(tags or "")
    return "\n".join(part for part in [d.title, d.kind, d.source_path, tag_text, d.text] if part)


def index_qdrant(
    docs: Iterable[SearchDocument],
    collection_name: str | None = None,
    *,
    replace_scope: str | None = None,
    replace_project_id: str | None = None,
) -> dict[str, Any]:
    docs = list(docs)
    client, status = qdrant_client()
    collection = collection_name or qdrant_collection_name()
    if client is None:
        return {"status": "skipped", "backend": "qdrant", "reason": status, "document_count": 0, "embedding": embedding_profile()}
    try:
        from qdrant_client.models import PointStruct

        vectors: list[list[float]] = []
        dim = vector_size()
        if docs:
            texts = [_doc_embedding_text(d) for d in docs]
            vectors = embed_texts(texts, is_query=False)
            if len(vectors) != len(docs):
                raise RuntimeError(f"embedding provider returned {len(vectors)} vectors for {len(docs)} documents")
            dim = len(vectors[0]) if vectors else vector_size()
            collection = collection_name or qdrant_collection_name(dim)
            ensure_qdrant_collection(client, collection, dim)
        elif replace_scope:
            # No fresh docs is still meaningful: clear stale points for this scope.
            clear_result = clear_qdrant_points(scope=replace_scope, project_id=replace_project_id, collection_name=collection)
            return {"status": "indexed", "backend": "qdrant", "collection": collection, "document_count": 0, "embedding": embedding_profile(), "clear": clear_result}

        clear_result = None
        if replace_scope:
            clear_result = clear_qdrant_points(scope=replace_scope, project_id=replace_project_id, collection_name=collection)
            if clear_result.get("status") in {"error", "rejected"}:
                return {"status": "error", "backend": "qdrant", "reason": "failed to clear stale scoped points before reindex", "clear": clear_result, "document_count": 0, "embedding": embedding_profile()}
        points = []
        profile = embedding_profile()
        for d, vector in zip(docs, vectors, strict=True):
            payload = {
                "id": d.id,
                "scope": d.scope,
                "kind": d.kind,
                "project_id": d.project_id,
                "source_path": d.source_path,
                "line": d.line,
                "title": d.title,
                "preview": preview(d.text),
                "metadata": d.metadata,
                "embedding": profile,
                "embedding_dim": len(vector),
            }
            points.append(
                PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, d.id)),
                    vector=vector,
                    payload=payload,
                )
            )
        if points:
            client.upsert(collection_name=collection, points=points, wait=True)
        return {
            "status": "indexed",
            "backend": "qdrant",
            "collection": collection,
            "document_count": len(points),
            "vector_size": dim,
            "embedding": profile,
            "clear": clear_result,
        }
    except Exception as exc:  # pragma: no cover - depends on runtime server/models
        return {"status": "error", "backend": "qdrant", "reason": str(exc), "document_count": 0, "embedding": embedding_profile()}


def search_qdrant(
    query: str,
    scope: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    memory_only: bool = False,
    limit: int = 10,
    collection_name: str | None = None,
) -> list[dict[str, Any]]:
    client, status = qdrant_client()
    if client is None:
        return []
    try:
        query_filter = _qdrant_filter(
            scope=scope,
            project_id=project_id,
            kind=kind,
            memory_only=memory_only,
        )
        vec = embed_query(query)
        collection = collection_name or qdrant_collection_name(len(vec))
        try:
            response = client.query_points(collection_name=collection, query=vec, query_filter=query_filter, limit=limit)
            points = getattr(response, "points", response)
        except AttributeError:
            points = client.search(collection_name=collection, query_vector=vec, query_filter=query_filter, limit=limit)
        hits: list[dict[str, Any]] = []
        for p in points:
            payload = getattr(p, "payload", {}) or {}
            hits.append(
                {
                    "retrieval_backend": "qdrant",
                    "id": payload.get("id") or str(getattr(p, "id", "")),
                    "scope": payload.get("scope"),
                    "kind": payload.get("kind"),
                    "project_id": payload.get("project_id"),
                    "source_path": payload.get("source_path"),
                    "line": payload.get("line"),
                    "title": payload.get("title"),
                    "preview": payload.get("preview", ""),
                    "score": float(getattr(p, "score", 0.0) or 0.0),
                    "metadata": payload.get("metadata", {}),
                    "embedding": payload.get("embedding"),
                    "embedding_dim": payload.get("embedding_dim"),
                }
            )
        return hits
    except Exception:  # pragma: no cover - qdrant/model may be down/empty
        return []


def _hit_key(h: dict[str, Any]) -> str:
    if h.get("source_path") is not None or h.get("line") is not None:
        return f"{h.get('scope')}:{h.get('kind')}:{h.get('source_path')}:{h.get('line')}"
    return str(h.get("id") or f"{h.get('source_path')}:{h.get('line')}:{h.get('title')}")


def merge_hits(*hit_lists: Iterable[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Fuse FTS, vector, and legacy hits with weighted reciprocal-rank fusion."""
    weights = {
        "sqlite_fts": 1.25,
        "qdrant": 1.0,
        "jsonl_scan": 0.55,
        "legacy": 0.55,
    }
    k = _env_int("AWOKI_RRF_K", 60, 10, 200)
    merged: dict[str, dict[str, Any]] = {}
    for hits in hit_lists:
        for rank, h in enumerate(hits, start=1):
            key = _hit_key(h)
            backend = h.get("retrieval_backend", "legacy")
            score = weights.get(str(backend), 0.5) / (k + rank)
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(h)
                merged[key]["score"] = score
                merged[key]["rrf_score"] = score
                merged[key]["raw_scores"] = {str(backend): float(h.get("score", 0.0) or 0.0)}
                merged[key]["retrieval_backends"] = [backend]
            else:
                existing["score"] = float(existing.get("score", 0.0) or 0.0) + score
                existing["rrf_score"] = float(existing.get("rrf_score", 0.0) or 0.0) + score
                existing.setdefault("raw_scores", {})[str(backend)] = float(h.get("score", 0.0) or 0.0)
                if backend not in existing.get("retrieval_backends", []):
                    existing.setdefault("retrieval_backends", []).append(backend)
    out = list(merged.values())
    out.sort(key=lambda r: float(r.get("rrf_score", r.get("score", 0.0)) or 0.0), reverse=True)
    return out[: max(1, min(int(limit), 50))]


def rerank_enabled() -> bool:
    return _env_bool("AWOKI_RERANK_ENABLED", False)


def _rerank_api_key_state() -> tuple[str, str, str]:
    """Resolve reranker authentication without silently dropping configured indirection.

    Returns ``(key, source, error)``.  ``source`` is one of ``direct``,
    ``indirect``, or ``none``.  An explicitly configured but invalid or
    unavailable indirection is a configuration error so callers can fail
    before issuing an unauthenticated network request.
    """
    direct = os.environ.get("AWOKI_RERANK_API_KEY", "").strip()
    if direct:
        return direct, "direct", ""

    env_name = os.environ.get("AWOKI_RERANK_API_KEY_ENV", "").strip()
    if not env_name:
        return "", "none", ""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name) is None:
        return "", "none", "AWOKI_RERANK_API_KEY_ENV is not a valid environment variable name"

    indirect = os.environ.get(env_name, "").strip()
    if not indirect:
        return "", "none", f"configured reranker credential environment variable {env_name} is unavailable"
    return indirect, "indirect", ""


def rerank_profile() -> dict[str, Any]:
    provider = os.environ.get("AWOKI_RERANK_PROVIDER", "http").strip().lower()
    if provider in {"openai", "openai_compatible", "remote", "url"}:
        provider = "http"
    if provider in {"text-embeddings-inference", "huggingface-tei", "hf-tei"}:
        provider = "tei"
    url = os.environ.get("AWOKI_RERANK_URL", "").strip()
    model = os.environ.get("AWOKI_RERANK_MODEL", DEFAULT_RERANK_MODEL).strip()
    api_key, api_key_source, auth_configuration_error = _rerank_api_key_state()
    enabled = rerank_enabled()
    return {
        "enabled": enabled,
        "provider": provider,
        "url": url,
        "endpoint_configured": bool(url),
        "auth_configured": bool(api_key),
        "auth_key_source": api_key_source,
        "auth_configuration_error": auth_configuration_error,
        "configuration_ready": (not enabled) or (bool(url) and not auth_configuration_error),
        "model": model,
        "server_selects_model": provider == "tei" and not model,
        "candidate_limit": _env_int("AWOKI_RERANK_CANDIDATES", 30, 1, 200),
        "top_n": _env_int("AWOKI_RERANK_TOP_N", 10, 1, 50),
        "timeout_seconds": _env_int("AWOKI_RERANK_TIMEOUT_SECONDS", 20, 1, 300),
        "max_document_chars": _env_int("AWOKI_RERANK_MAX_DOCUMENT_CHARS", 4000, 256, 20000),
        "fail_mode": os.environ.get("AWOKI_RERANK_FAIL_MODE", "fallback").strip().lower(),
    }


def _hit_rerank_text(hit: dict[str, Any], max_chars: int = 4000) -> str:
    fields = [
        str(hit.get("title") or ""),
        str(hit.get("kind") or ""),
        str(hit.get("source_path") or ""),
        str(hit.get("preview") or ""),
    ]
    return "\n".join(f for f in fields if f)[:max_chars]


def _remote_rerank_scores(query: str, documents: list[str], profile: dict[str, Any]) -> list[tuple[int, float]]:
    url = str(profile.get("url") or "").strip()
    if not url:
        raise RuntimeError("AWOKI_RERANK_URL is required when remote reranking is enabled")
    api_key, _api_key_source, auth_configuration_error = _rerank_api_key_state()
    if auth_configuration_error:
        raise RuntimeError(auth_configuration_error)
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - dependency is required in normal installs
        raise RuntimeError("httpx is required for AWOKI_RERANK_PROVIDER=http|tei") from exc
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    provider = str(profile.get("provider") or "http").lower()
    top_n = min(int(profile.get("top_n") or len(documents)), len(documents))
    if provider == "tei":
        # Hugging Face Text Embeddings Inference native rerank contract.
        payload: dict[str, Any] = {
            "query": query,
            "texts": documents,
            "raw_scores": False,
        }
    else:
        payload = {
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        if profile.get("model"):
            payload["model"] = profile["model"]
    response = httpx.post(
        url,
        json=payload,
        headers=headers,
        timeout=float(profile.get("timeout_seconds") or 20),
        follow_redirects=False,
    )
    response.raise_for_status()
    body = response.json()
    rows = body.get("results") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise RuntimeError("reranker response must contain a results list")
    parsed: list[tuple[int, float]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        index = row.get("index", row.get("document_index", row.get("id", position)))
        score = row.get("relevance_score", row.get("score", row.get("similarity")))
        try:
            idx = int(index)
            numeric = float(score)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(documents):
            parsed.append((idx, numeric))
    if not parsed:
        raise RuntimeError("reranker response contained no usable index/score pairs")
    return sorted(parsed, key=lambda item: item[1], reverse=True)[:top_n]


def rerank_hits(
    query: str,
    hits: list[dict[str, Any]],
    limit: int = 10,
    *,
    timeout_override: float | None = None,
    top_n_override: int | None = None,
) -> list[dict[str, Any]]:
    if not hits or not rerank_enabled():
        return hits[:limit]
    profile = rerank_profile()
    if timeout_override is not None:
        profile["timeout_seconds"] = max(0.25, min(float(profile["timeout_seconds"]), float(timeout_override)))
    if top_n_override is not None:
        # Code-search may intentionally request a score for every selected
        # reranker candidate even when the operator-facing display/default
        # top-N is smaller. Keep this bounded by the same hard candidate ceiling
        # rather than the user-facing AWOKI_RERANK_TOP_N=50 configuration cap.
        profile["top_n"] = max(1, min(int(top_n_override), 200))
    provider = str(profile["provider"]).lower()
    candidate_limit = int(profile["candidate_limit"])
    candidates = hits[:candidate_limit]
    try:
        if provider not in {"http", "tei"}:
            raise RuntimeError(
                f"unsupported rerank provider {provider!r}; "
                "use AWOKI_RERANK_PROVIDER=http|tei"
            )
        documents = [_hit_rerank_text(h, int(profile["max_document_chars"])) for h in candidates]
        global _LAST_RERANK_ERROR
        scored = _remote_rerank_scores(query, documents, profile)
        _LAST_RERANK_ERROR = ""
        seen: set[int] = set()
        reranked: list[dict[str, Any]] = []
        for idx, score in sorted(scored, key=lambda item: item[1], reverse=True):
            if idx in seen:
                continue
            seen.add(idx)
            item = dict(candidates[idx])
            item["rerank_backend"] = "remote_http"
            item["rerank_model"] = profile["model"]
            item["rerank_score"] = float(score)
            item["pre_rerank_score"] = float(item.get("score", 0.0) or 0.0)
            item["score"] = float(score)
            reranked.append(item)
        # Preserve candidates omitted by top_n and all non-candidate tail results.
        reranked.extend(dict(candidates[idx]) for idx in range(len(candidates)) if idx not in seen)
        reranked.extend(dict(item) for item in hits[candidate_limit:])
        return reranked[: max(1, min(int(limit), 50))]
    except Exception as exc:
        _LAST_RERANK_ERROR = str(exc)[:1000]
        if str(profile.get("fail_mode")) == "error":
            raise
        out = [dict(h) for h in hits[:limit]]
        for h in out:
            h["rerank_error"] = str(exc)
            h["rerank_fallback"] = True
        return out
