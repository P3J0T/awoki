"""Short source handles in the existing non-RAG evidence store, not a new ledger.

A handle is navigation, not proof. Resolution always precedes the normal source
verifier. Only the token and display metadata are persisted, never source text.
"""
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import evidence_store
import work_ledger
import continuity

KIND = "code_source_reference"
TOOL = "code_source_window"
BOUNDARY = "Source identity/freshness only; not verification of a behavioral claim or runtime execution."


def capture(root: Path, project_id: str, result: dict[str, Any], *, session_id: str = "") -> dict[str, Any]:
    token = (result.get("evidence") or {}).get("evidence_id")
    if result.get("status") != "ok" or not isinstance(token, str) or not token:
        return result
    locator = dict(result.get("evidence_locator") or {})
    scope = {key: result.get(key) for key in ("project_id", "repo_id", "source_id", "revision_key")}
    identity = result.get("repo_id") or result.get("source_id")
    returned = result["returned"]
    citation = f"{identity}/{result['path']}:{returned['start_line']}-{returned['end_line']}"
    payload = {
        "evidence_id": token, "citation": citation, "evidence_locator": locator,
        "truncated": bool(result.get("truncated")), "redacted": bool(result.get("redacted")),
        "verification_boundary": BOUNDARY,
    }
    output = {**result, "citation": citation, "verification_boundary": BOUNDARY}
    try:
        stored = evidence_store.put(root, project_id, kind=KIND, tool=TOOL, payload=payload,
                                    scope_identity=scope, session_id=session_id)
    except OSError:
        # Reading source must remain possible on a read-only/full evidence volume.
        stored = {"status": "unavailable", "reason": "source_reference_storage_failed"}
    if stored.get("status") != "stored":
        output["evidence_reference_warning"] = str(stored.get("reason") or stored.get("status"))
        return output
    output["evidence_ref"] = stored["evidence_ref"]
    output["reopen_call"] = {"tool": TOOL, "arguments": {"name": project_id, "evidence_ref": stored["evidence_ref"]}}
    output["path_format"] = "path is relative to the selected repo/source root; citation is a display label, not a path"
    if session_id:
        try:
            work_ledger.touch_reference(root, session_id, project_id=project_id,
                                       reference_id=stored["evidence_ref"], label=citation,
                                       why_saved="Source window; reverify before relying on freshness.")
        except OSError:
            output["evidence_reference_warning"] = "session_reference_tracking_failed"
    return output


def resolve(root: Path, project_id: str, reference: str) -> tuple[str, dict[str, Any] | None]:
    """Return a validated stored token or an explicit failure; never fuzzy-match."""
    meta = evidence_store.metadata(root, project_id, reference)
    if (meta.get("status") != "ok" or meta.get("kind") != KIND
            or meta.get("tool") != TOOL or meta.get("project_id") != project_id):
        return "", {"status": "rejected", "verdict": "INVALID_EVIDENCE",
                    "evidence_ref": reference, "reason": "Missing, corrupt, wrong-project or non-source evidence reference."}
    saved = evidence_store.get(root, project_id, reference, selector="payload.evidence_id", max_chars=16000)
    token = saved.get("value")
    if saved.get("status") != "ok" or saved.get("truncated") or not isinstance(token, str) or not token:
        return "", {"status": "rejected", "verdict": "INVALID_EVIDENCE",
                    "evidence_ref": reference, "reason": "Source reference does not contain a complete evidence token."}
    return token, None


def reopen(paths: Any, project_id: str, reference: str, *, repo: str = "", source_id: str = "",
           max_chars: int = 20000, max_line_chars: int = 4096, session_id: str = "") -> dict[str, Any]:
    """Reopen only the exact saved window while its source identity is current."""
    from code_search import engine, provenance

    token, error = resolve(paths.root, project_id, reference)
    if error:
        return error
    verified = engine.verify_evidence(paths, project_id, token, repo=repo, source=source_id)
    if verified.get("current") is not True:
        return {**verified, "evidence_ref": reference, "verification_boundary": BOUNDARY,
                "reopened": False}
    payload = provenance.decode_evidence(token)
    result = engine.source_window(
        paths, project_id, payload["path"], start_line=payload["start_line"], end_line=payload["end_line"],
        repo=str(verified.get("repo_id") or ""), source=str(verified.get("source_id") or ""),
        max_chars=max_chars, max_line_chars=max_line_chars,
    )
    if result.get("status") != "ok":
        return {**result, "evidence_ref": reference, "reopened": False}
    # Do not return newly indexed bytes under an older reference if the source
    # changed between verification and the bounded read.
    after = engine.verify_evidence(paths, project_id, token, repo=repo, source=source_id)
    if after.get("current") is not True or result.get("source_sha256") != payload.get("source_sha256"):
        return {"status": "stale", "verdict": "STALE_SOURCE", "evidence_ref": reference,
                "reopened": False, "reason": "Source identity changed while reopening the saved window."}
    return {**capture(paths.root, project_id, result, session_id=session_id),
            "reopened": True, "reopened_from": reference}


def memory_source_issues(root: Path, project_id: str, sources: list[Any]) -> list[dict[str, str]]:
    """Validate declared handles before capture/reconciliation; never infer truth.

    Generic evidence is allowed. Stale source bytes remain valid historical
    evidence; freshness is checked separately at recall. Plain notes/paths need
    no artifact. We never search other projects or fuzzy-match a missing ID.
    """
    issues = []
    cache = {}
    for item in continuity.normalize_sources(sources):
        ref = str(item.get("id") or "")
        if not ref.startswith("ev_"):
            continue
        if ref not in cache:
            cache[ref] = evidence_store.metadata(root, project_id, ref)
        meta = cache[ref]
        reason = ""
        if meta.get("status") != "ok" or meta.get("project_id") != project_id:
            reason = "missing_corrupt_or_wrong_project"
        elif meta.get("kind") == KIND:
            scope = meta.get("scope_identity") or {}
            stored = evidence_store.get(root, project_id, ref, selector="payload.evidence_locator", max_chars=4000)
            locator = stored.get("value")
            if stored.get("status") != "ok" or not isinstance(locator, dict):
                reason = "invalid_source_locator"
            elif any(item.get(key) and str(item[key]) != str(scope.get(key) or "")
                     for key in ("source_id",)) or (item.get("repo") and item["repo"] != scope.get("repo_id")):
                reason = "source_scope_mismatch"
            elif item.get("path") and item["path"] != locator.get("path"):
                reason = "source_path_mismatch"
            elif any(key in item and item[key] != locator.get(bound)
                     for key, bound in (("line_start", "start_line"), ("line_end", "end_line"))):
                reason = "source_range_mismatch"
        if reason:
            issues.append({"evidence_ref": ref, "reason": reason})
    return issues


def bind_memory_sources(root: Path, project_id: str, sources: list[Any]) -> list[dict[str, Any]]:
    """Fill missing source labels from saved artifacts, never guess them from prose."""
    out = continuity.normalize_sources(sources)
    for item in out:
        reference = str(item.get("id") or "")
        if not reference.startswith("ev_"):
            continue
        meta = evidence_store.metadata(root, project_id, reference)
        if meta.get("status") != "ok" or meta.get("kind") != KIND:
            continue
        scope = meta.get("scope_identity") or {}
        if scope.get("repo_id"):
            item.setdefault("repo", scope["repo_id"])
        elif scope.get("source_id"):
            item.setdefault("source_id", scope["source_id"])
        stored = evidence_store.get(root, project_id, reference, selector="payload.evidence_locator", max_chars=4000)
        locator = stored.get("value") if stored.get("status") == "ok" and not stored.get("truncated") else None
        if isinstance(locator, dict) and locator.get("path"):
            item.setdefault("path", locator["path"])
            for source_key, locator_key in (("line_start", "start_line"), ("line_end", "end_line")):
                if isinstance(locator.get(locator_key), int):
                    item.setdefault(source_key, locator[locator_key])
    return continuity.normalize_sources(out)


def annotate_memory(root: Path, project_id: str, records: list[dict[str, Any]], *,
                    max_checks: int = 16) -> list[dict[str, Any]]:
    """Read-time, bounded source checks; never promote a claim or mutate memory.

    Only saved short source handles are resolvable here. Paths, prose, external
    URLs and user assertions remain useful memory, explicitly unverified. The
    cache lives for this call only: another checkout change requires new checks.
    """
    from code_search.engine import verify_evidence

    cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for record in records:
        checks: list[dict[str, Any]] = []
        sources = record.get("sources") or []
        for source in sources[:100]:
            source = source if isinstance(source, dict) else {"id": str(source)}
            reference = str(source.get("id") or source.get("ref") or "")
            repo = str(source.get("repo") or "")
            source_id = str(source.get("source_id") or "")
            key = (reference, repo, source_id)
            if not reference.startswith("ev_"):
                checks.append({"status": "unbound", "repo": repo,
                               "path": str(source.get("path") or "")})
                continue
            if key not in cache and len(cache) < max(0, min(max_checks, 64)):
                try:
                    token, error = resolve(root, project_id, reference)
                    result = error or verify_evidence(SimpleNamespace(root=root), project_id, token, repo=repo, source=source_id)
                    cache[key] = {
                        "status": "current" if result.get("current") is True else
                                  "stale" if result.get("status") == "stale" else "unverified",
                        "verdict": str(result.get("verdict") or "UNVERIFIED"),
                        "repo": str(result.get("repo_id") or result.get("source_id") or repo),
                        "path": str(result.get("path") or ""),
                    }
                except (OSError, ValueError, RuntimeError):
                    cache[key] = {"status": "unverified", "verdict": "CHECK_UNAVAILABLE"}
            checks.append({"evidence_ref": reference,
                           "reopen_call": {"tool": TOOL, "arguments": {"name": project_id, "evidence_ref": reference}},
                           **cache.get(key, {"status": "not_checked", "verdict": "CHECK_BUDGET_EXCEEDED"})})
        statuses = {check["status"] for check in checks}
        status = ("stale" if "stale" in statuses else "current" if statuses == {"current"}
                  else "unverified" if checks else "unbound")
        out.append({**record, "source_freshness": {
            "status": status, "checks": checks, "checked_at": continuity.now_ts(),
            "claim_verified": False, "verification_boundary": BOUNDARY,
        }})
    return out
