from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import threading
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import rag_backend
import burp
import project_workspace
import continuity_migration
import continuity
import indexing_policy
import safety
import code_search
import code_vector_jobs
import code_index_jobs
import repository_prepare_jobs
import continuations
import work_ledger
import acceptance_runs
import evidence_store
import agent_runtime
import reference_catalog

PROJECT_ONLY_HINTS = {
    "client", "customer", "staging", "prod", "production", "internal", "vpn",
    "private", "sha256", "md5", "ip", "endpoint", "repo", "path", "binary",
}

GLOBAL_HINTS = {
    "prefer", "workflow", "recipe", "procedure", "always", "never", "when auditing",
    "when testing", "heuristic", "pattern", "rule", "methodology", "triage",
}

DEFAULT_GLOBAL_ROOT = "~/.awoki"

def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


@dataclass
class HarnessPaths:
    root: Path
    global_root: Path

    @classmethod
    def from_env(cls) -> "HarnessPaths":
        root = expand(os.environ.get("AWOKI_ROOT") or os.environ.get("HARNESS_ROOT", ".")).resolve()
        global_root = expand(os.environ.get("AWOKI_GLOBAL_ROOT") or os.environ.get("HARNESS_GLOBAL_ROOT", DEFAULT_GLOBAL_ROOT)).resolve()
        return cls(root=root, global_root=global_root)

    @property
    def manifest(self) -> Path:
        return self.root / ".harness" / "manifest.json"

    @property
    def memory_dir(self) -> Path:
        return self.root / ".harness" / "memory"

    @property
    def skills_dir(self) -> Path:
        return self.root / ".opencode" / "skills"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / ".harness" / "artifacts"

    @property
    def index_dir(self) -> Path:
        return self.root / ".harness" / "index"

    @property
    def state_dir(self) -> Path:
        return self.root / ".harness" / "state"

    @property
    def projects_dir(self) -> Path:
        return self.root / "workspace" / "projects"

    @property
    def global_index_dir(self) -> Path:
        return self.global_memory_dir

    @property
    def global_memory_dir(self) -> Path:
        return self.global_root / "global"

    @property
    def global_skills_dir(self) -> Path:
        # Keep OpenCode-native global skill path separate from global root.
        return expand(os.environ.get("AWOKI_GLOBAL_SKILLS_DIR") or os.environ.get("HARNESS_GLOBAL_SKILLS_DIR") or "~/.config/opencode/skills")


def ensure_dirs(paths: HarnessPaths) -> None:
    for p in [paths.memory_dir, paths.artifacts_dir, paths.index_dir, paths.state_dir, paths.projects_dir, paths.global_memory_dir, paths.global_skills_dir]:
        p.mkdir(parents=True, exist_ok=True)
    for name in ["memories.jsonl", "procedures.jsonl", "preferences.jsonl", "promotion_log.jsonl"]:
        (paths.global_memory_dir / name).touch(exist_ok=True)
    for name in ["project.jsonl", "findings.jsonl", "hypotheses.jsonl", "promotion_candidates.jsonl", "skill_update_candidates.jsonl"]:
        (paths.memory_dir / name).touch(exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                obj.setdefault("_source_file", str(path))
                obj.setdefault("_line", i)
                rows.append(obj)
        except json.JSONDecodeError as e:
            rows.append({"kind":"parse_error","text":f"Invalid JSONL in {path}:{i}: {e}","_source_file":str(path),"_line":i})
    return rows


def append_jsonl(path: Path, obj: dict[str, Any]) -> dict[str, Any]:
    """Append one JSON object safely.

    JSONL is the durable MVP store. Use an advisory lock on POSIX so multiple
    MCP/tool calls do not interleave writes, then fsync so reviewed memories are
    not lost on abrupt container shutdown.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = dict(obj)
    obj.setdefault("created_at", now_ts())
    with path.open("a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return obj


def redact_text(text: str) -> tuple[str, bool]:
    return safety.redact_text(text)


def redact_analysis_text(text: str) -> tuple[str, bool]:
    return safety.redact_analysis_text(text)


def classify_memory_text(text: str, project_id: str | None = None) -> dict[str, Any]:
    """Classify durable analysis without turning credential values into blind spots.

    High-confidence values may be redacted for ordinary storage, but that is a
    transport/storage treatment rather than a reason to suppress the finding or
    force it into secret/no-RAG memory. Verbatim sensitive plaintext remains an
    explicit user-directed mode.
    """
    lower = text.lower()
    redacted, changed = redact_analysis_text(text)
    has_project_hint = any(h in lower for h in PROJECT_ONLY_HINTS)
    has_global_hint = any(h in lower for h in GLOBAL_HINTS)

    if has_project_hint and not has_global_hint:
        result = {
            "decision": "project",
            "confidence": 0.86,
            "sensitivity": "internal",
            "promotion_candidate": False,
            "requires_review": False,
            "reason": "Contains project-specific indicators such as environment, endpoint, hash, path, or customer context.",
        }
    elif has_global_hint and not has_project_hint:
        result = {
            "decision": "global_candidate",
            "confidence": 0.78,
            "sensitivity": "normal",
            "promotion_candidate": True,
            "requires_review": True,
            "reason": "Looks like reusable procedure, preference, methodology, or heuristic.",
        }
    elif has_global_hint and has_project_hint:
        result = {
            "decision": "hybrid_candidate",
            "confidence": 0.68,
            "sensitivity": "internal",
            "promotion_candidate": True,
            "requires_review": True,
            "reason": "Contains a possible reusable lesson but also project-specific details. Generalize before global promotion.",
        }
    else:
        result = {
            "decision": "project",
            "confidence": 0.62,
            "sensitivity": "normal",
            "promotion_candidate": False,
            "requires_review": False,
            "reason": "Default local-first policy applies.",
        }
    result["redaction_recommended"] = bool(changed)
    if changed:
        result["redacted_preview"] = redacted
        result["redaction_note"] = (
            "A high-confidence credential value was masked best-effort; surrounding analysis remains eligible for normal continuity/retrieval."
        )
    return result


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_./:-]{2,}", text.lower()))


def score_record(query: str, record: dict[str, Any]) -> float:
    q = tokenize(query)
    if not q:
        return 0.1
    text_parts = []
    for key in ["title", "summary", "details", "text", "hypothesis", "evidence", "tags", "kind", "scope"]:
        val = record.get(key)
        if isinstance(val, list):
            text_parts.append(" ".join(map(str, val)))
        elif val is not None:
            text_parts.append(str(val))
    blob = " ".join(text_parts).lower()
    tokens = tokenize(blob)
    overlap = q & tokens
    substring_hits = sum(1 for term in q if term in blob)
    return len(overlap) * 2.0 + substring_hits * 0.5


def search_records(query: str, records: Iterable[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    scored = []
    for rec in records:
        s = score_record(query, rec)
        if query.strip() == "" or s > 0:
            item = dict(rec)
            item["score"] = s
            scored.append(item)
    scored.sort(key=lambda r: (r.get("score",0), r.get("created_at", "")), reverse=True)
    return scored[: max(1, min(limit, 50))]


def load_manifest(paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    if not paths.manifest.exists():
        return {"error": f"manifest not found: {paths.manifest}"}
    return read_json(paths.manifest)



def active_project_id(paths: HarnessPaths, manifest: dict[str, Any] | None = None, session_id: str | None = None) -> str:
    manifest = manifest if manifest is not None else load_manifest(paths)
    auto_tokens = {"", "__auto__", "auto", "awoki-template"}
    env_project = os.environ.get("AWOKI_PROJECT_ID") or os.environ.get("HARNESS_PROJECT_ID") or ""
    if env_project.strip() not in auto_tokens:
        return env_project.strip()
    session_project = project_workspace.current_project_id(paths.root, session_id=session_id)
    if session_project:
        return session_project
    manifest_project = str(manifest.get("active_project_id") or "")
    if manifest_project.strip() not in auto_tokens:
        return manifest_project.strip()
    return paths.root.name


def attached_project_id(paths: HarnessPaths, session_id: str | None = None) -> str | None:
    return project_workspace.current_project_id(paths.root, session_id=session_id)


def project_workspace_path(paths: HarnessPaths, project_id: str | None = None, session_id: str | None = None) -> project_workspace.ProjectPaths | None:
    pid = project_id or attached_project_id(paths, session_id=session_id)
    if not pid:
        return None
    try:
        pp = project_workspace.paths_for(paths.root, pid)
    except ValueError:
        return None
    return pp if pp.project_json.exists() else None


def harness_status(session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    manifest = load_manifest(paths)
    project_id = active_project_id(paths, manifest, session_id=session_id or None)
    skill_count = len(list(paths.skills_dir.glob("*/SKILL.md"))) if paths.skills_dir.exists() else 0
    global_skill_count = len(list(paths.global_skills_dir.glob("*/SKILL.md"))) if paths.global_skills_dir.exists() else 0
    return {
        "active_project": project_id,
        "attached_project": attached_project_id(paths, session_id=session_id or None),
        "project_root": str(paths.root),
        "projects_dir": str(paths.projects_dir),
        "harness_version": manifest.get("harness_version"),
        "runtime_mode": os.environ.get("AWOKI_MODE") or os.environ.get("HARNESS_MODE", "local"),
        "safety_mode": "continuity_first_explicit_sensitive_memory",
        "memory_scopes": ["session", "project", "global"],
        "project_rules_path": str(paths.root / "AGENTS.md"),
        "manifest_path": str(paths.manifest),
        "project_skill_count": skill_count,
        "global_skill_count": global_skill_count,
        "sensitive_memory_policy": manifest.get("memory_policy", {}).get("sensitive_memory", "explicit_only_no_rag"),
        "promotion_policy": "review_required",
        "recommended_next_calls": ["project_open", "project_status", "project_search"],
        "warning": "Project attachment is isolated by OpenCode session. Use project_open for named work; no legacy fallback is used.",
    }


def project_records(paths: HarnessPaths, session_id: str | None = None) -> list[dict[str, Any]]:
    """Return continuity-first records for the project attached to this session.

    An unattached caller receives no project records. Legacy root-level stores
    are available only through explicit migration/compatibility paths and never
    participate in normal continuity reads.
    """
    rows: list[dict[str, Any]] = []
    pp = project_workspace_path(paths, session_id=session_id)
    if pp is None:
        return rows

    rows.extend(project_workspace.continuity_records(pp))
    for md in [pp.situation, pp.handoff, pp.notes_dir / "thoughts.md", pp.project_dir / "README.md", pp.project_dir / "AGENTS.md"]:
        if md.exists():
            rows.append({
                "scope": "project",
                "project_id": pp.project_id,
                "kind": md.stem.lower(),
                "summary": md.name,
                "title": md.name,
                "text": md.read_text(encoding="utf-8", errors="replace"),
                "index_policy": "safe",
                "sensitivity": "project",
                "_source_file": str(md),
            })
    return rows

def global_demoted_memory_lines(paths: HarnessPaths) -> set[int]:
    demoted: set[int] = set()
    for rec in read_jsonl(paths.global_memory_dir / "promotion_log.jsonl"):
        if rec.get("kind") == "global_memory_demotion" and rec.get("global_line") is not None:
            try:
                demoted.add(int(rec["global_line"]))
            except (TypeError, ValueError):
                continue
    return demoted


def global_records(paths: HarnessPaths, *, include_sensitive: bool = False, include_burp: bool = False) -> list[dict[str, Any]]:
    ensure_dirs(paths)
    rows: list[dict[str, Any]] = []
    demoted_lines = global_demoted_memory_lines(paths)
    for name in ["memories.jsonl", "procedures.jsonl", "preferences.jsonl"]:
        for rec in read_jsonl(paths.global_memory_dir / name):
            if name == "memories.jsonl" and rec.get("_line") in demoted_lines:
                continue
            rows.append(rec)
    # Burp is an explicit adapter, not generic project/global memory. Keep its
    # inventories out of ordinary recall unless a Burp workflow asks for them.
    if include_burp:
        try:
            rows.extend(burp.burp_records_for_rag())
        except Exception as exc:
            rows.append({"scope":"global","kind":"burp_inventory_error","text":f"Burp inventory unavailable: {exc}"})
    if not include_sensitive:
        rows = [r for r in rows if not _is_sensitive_record(r)]
    return rows



def _is_sensitive_record(record: dict[str, Any]) -> bool:
    return (
        str(record.get("index_policy") or "safe").lower() == "no_rag"
        or str(record.get("sensitivity") or "project").lower() in {"sensitive", "secret"}
        or bool(record.get("explicit_sensitive_plaintext"))
    )


def record_text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in [
        "title", "summary", "text", "details", "hypothesis", "evidence", "reason",
        "label", "kind", "scope", "confidence", "status", "likely_continuation",
    ]:
        val = record.get(key)
        if isinstance(val, list):
            parts.append(" ".join(map(str, val)))
        elif val is not None:
            parts.append(str(val))
    for key in ["tags", "allowed_use", "uncertainty"]:
        val = record.get(key)
        if isinstance(val, list):
            parts.append(" ".join(map(str, val)))
    sources = record.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                parts.append(" ".join(str(source.get(k, "")) for k in ("type", "path", "ref", "line")))
            elif isinstance(source, str):
                parts.append(source)
    # Do not include secret_ref values in indexed text; labels/allowed_use are enough.
    return "\n".join(p for p in parts if p)


def _safe_metadata(record: dict[str, Any]) -> dict[str, Any]:
    blocked = {"secret_ref"}
    out: dict[str, Any] = {}
    for k, v in record.items():
        if k.startswith("_") or k in blocked:
            continue
        safe, _ = _redact_analysis_nested(v)
        out[k] = safe
    for key in ("text", "details", "evidence"):
        if key in out and isinstance(out[key], str) and len(out[key]) > 500:
            out[key] = out[key][:500] + "…"
    return out


def _record_index_reason(record: dict[str, Any]) -> str:
    if str(record.get("index_policy") or "safe").lower() != "safe":
        return f"index_policy:{record.get('index_policy')}"
    if str(record.get("sensitivity") or "project").lower() in {"sensitive", "secret"}:
        return f"sensitivity:{record.get('sensitivity')}"
    # Analysis/continuity records are coverage-first. Security vocabulary or
    # quoted code snippets must not make a finding disappear from retrieval.
    # Actual high-confidence values are sanitized when the document is built.
    return ""


def _record_to_doc(record: dict[str, Any], scope: str, project_id: str | None) -> rag_backend.SearchDocument | None:
    if _record_index_reason(record):
        return None
    source_path = record.get("_source_file") or f"{scope}:memory"
    line = record.get("_line")
    kind = str(record.get("kind") or "memory")
    title = safety.redact_analysis_text(str(record.get("title") or record.get("summary") or record.get("hypothesis") or record.get("label") or kind))[0]
    text = safety.redact_analysis_text(record_text(record))[0]
    return rag_backend.SearchDocument(
        id=rag_backend.stable_doc_id(scope, source_path, str(line), str(record.get("id") or ""), kind, title),
        scope=scope,
        kind=kind,
        title=title,
        text=text,
        source_path=str(source_path),
        line=int(line) if isinstance(line, int) else None,
        project_id=project_id,
        metadata={**_safe_metadata(record), "memory_record": True},
    )


def _text_file_documents(
    paths: HarnessPaths,
    roots: Iterable[Path],
    scope: str,
    project_id: str | None,
    kind: str,
    *,
    strict_artifacts: bool = False,
    registered_safe: Iterable[str] = (),
    decisions: list[dict[str, Any]] | None = None,
) -> list[rag_backend.SearchDocument]:
    # Do not hide semantically meaningful project material by arbitrary path
    # names such as build/dist/target. File policy decides eligibility; this
    # prefilter only avoids harness/cache internals that would otherwise create
    # recursion/noise. Repository code uses its dedicated structural index.
    ignore_parts = {".git", ".venv", "__pycache__", ".pytest_cache"}
    if kind == "code":
        ignore_parts |= {".harness", ".opencode"}
    docs: list[rag_backend.SearchDocument] = []
    seen: set[Path] = set()
    for base in roots:
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else base.rglob("*")
        for f in candidates:
            if not f.is_file() or f in seen:
                continue
            seen.add(f)
            try:
                rel = f.relative_to(paths.root)
            except ValueError:
                rel = f
            if set(rel.parts) & ignore_parts:
                continue
            decision = indexing_policy.decide_file(
                f,
                rel=rel,
                category=kind,
                redact=redact_text,
                registered_safe=registered_safe,
                strict_artifacts=strict_artifacts,
            )
            if decisions is not None:
                decisions.append(decision.as_dict())
            if not decision.included:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            chunk_size = 8000
            sanitizer = safety.redact_source_text if kind == "code" else safety.redact_analysis_text
            for idx in range(0, max(1, len(text)), chunk_size):
                chunk, chunk_redacted = sanitizer(text[idx: idx + chunk_size])
                if not chunk.strip():
                    continue
                title = f"{kind}: {rel}"
                docs.append(rag_backend.SearchDocument(
                    id=rag_backend.stable_doc_id(scope, str(rel), kind, decision.content_hash, str(idx)),
                    scope=scope,
                    kind=kind,
                    title=title,
                    text=chunk,
                    source_path=str(rel),
                    line=None,
                    project_id=project_id,
                    metadata={
                        "chunk_start": idx,
                        "file_size": decision.size_bytes,
                        "content_hash": decision.content_hash,
                        "index_policy": "safe",
                        "redacted": bool(chunk_redacted),
                    },
                ))
    return docs


def _collect_project_documents_with_plan(
    paths: HarnessPaths,
    include_artifacts: bool = False,
    include_code: bool = False,
    include_skills: bool = False,
    project_id: str | None = None,
    session_id: str | None = None,
) -> tuple[list[rag_backend.SearchDocument], list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect the exact fail-closed document set for one project.

    Caller flags request categories; project.json policy is the upper bound.
    Disabling a category in project policy always wins.
    """
    pp = project_workspace_path(paths, project_id=project_id, session_id=session_id)
    effective_project_id = pp.project_id if pp else active_project_id(paths, session_id=session_id)
    docs: list[rag_backend.SearchDocument] = []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    rag_policy: dict[str, Any] = {}
    if pp is not None:
        try:
            project_meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            project_meta = {}
        rag_policy = project_meta.get("rag") if isinstance(project_meta.get("rag"), dict) else {}

    records = project_workspace.continuity_records(pp) if pp is not None else project_records(paths, session_id=session_id)
    memory_allowed = bool(rag_policy.get("index_memory", True)) if pp is not None else True
    for record in records:
        source = str(record.get("_source_file") or (pp.continuity if pp else "project:memory"))
        entry = {
            "path": source,
            "record_id": record.get("id"),
            "kind": record.get("kind"),
            "content_hash": record.get("fingerprint") or rag_backend.stable_doc_id(record_text(record)),
        }
        if not memory_allowed:
            entry["reason"] = "project_policy:index_memory=false"
            excluded.append(entry)
            continue
        doc = _record_to_doc(record, "project", effective_project_id)
        if doc is None:
            entry["reason"] = _record_index_reason(record)
            excluded.append(entry)
        else:
            entry["reason"] = "safe_record"
            included.append(entry)
            docs.append(doc)

    file_decisions: list[dict[str, Any]] = []
    if pp is not None:
        base_files: list[Path] = []
        if bool(rag_policy.get("index_situation", True)):
            base_files.append(pp.situation)
        if bool(rag_policy.get("index_handoff", True)):
            base_files.append(pp.handoff)
        if bool(rag_policy.get("index_notes", True)):
            base_files.append(pp.notes_dir / "thoughts.md")
        # Project identity/rules are small, generated or user-maintained safe text.
        base_files.extend([pp.project_dir / "README.md", pp.project_dir / "AGENTS.md"])
        docs.extend(_text_file_documents(paths, base_files, "project", effective_project_id, "project_view", decisions=file_decisions))

        if include_artifacts:
            artifact_roots: list[Path] = []
            if bool(rag_policy.get("index_corpora", True)):
                artifact_roots.append(pp.corpora_dir)
            if bool(rag_policy.get("index_reports", True)):
                artifact_roots.append(pp.project_dir / "reports")
            if artifact_roots:
                docs.extend(_text_file_documents(paths, artifact_roots, "project", effective_project_id, "artifact", decisions=file_decisions))
            if bool(rag_policy.get("index_registered_artifacts", True)):
                registered = indexing_policy.read_safe_artifact_registry(pp.index_dir)
                docs.extend(_text_file_documents(
                    paths,
                    [pp.artifacts_dir],
                    "project",
                    effective_project_id,
                    "artifact",
                    strict_artifacts=True,
                    registered_safe=registered,
                    decisions=file_decisions,
                ))
            else:
                excluded.append({"path": str(pp.artifacts_dir), "reason": "project_policy:index_registered_artifacts=false", "kind": "artifact_root"})
        if include_code and bool(rag_policy.get("index_code", False)):
            # Repository source has one owner: the structural code index. The
            # general project-RAG preview reuses each registered repository's
            # eligibility scan but never creates fixed-window code documents.
            for repository in project_workspace.project_repositories(paths.root, effective_project_id):
                rid = str(repository.get("repo_id") or "")
                code_preview = code_search.preview_project_code(paths, effective_project_id, repo=rid)
                for decision in code_preview.get("included") or []:
                    item = dict(decision)
                    item.setdefault("repo_id", rid)
                    file_decisions.append(item)
                for decision in code_preview.get("excluded") or []:
                    item = dict(decision)
                    item.setdefault("repo_id", rid)
                    file_decisions.append(item)
        elif include_code:
            excluded.append({"path": str(pp.project_dir / "repo"), "reason": "project_policy:index_code=false", "kind": "code_root"})
    else:
        if include_artifacts:
            docs.extend(_text_file_documents(paths, [paths.artifacts_dir, paths.root / "corpora", paths.root / "docs"], "project", effective_project_id, "artifact", decisions=file_decisions))
        if include_code:
            excluded.append({
                "path": str(paths.root),
                "reason": "structural_code_index_requires_workspace_project",
                "kind": "code_root",
            })

    for decision in file_decisions:
        (included if decision.get("included") else excluded).append(decision)

    skills_allowed = bool(rag_policy.get("index_skills", False)) if pp is not None else include_skills
    if include_skills and skills_allowed:
        for skill in search_skills("", scope="project", limit=50, paths=paths):
            doc = rag_backend.SearchDocument(
                id=rag_backend.stable_doc_id("project", "skill", skill.get("name", ""), skill.get("path", "")),
                scope="project",
                kind="skill",
                title=f"skill: {skill.get('name')}",
                text=f"{skill.get('name')}\n{skill.get('description')}\n{' '.join(skill.get('tags', []))}",
                source_path=str(skill.get("path", "")),
                project_id=effective_project_id,
                metadata={k: v for k, v in skill.items() if k != "text"},
            )
            docs.append(doc)
            included.append({"path": str(skill.get("path", "")), "kind": "skill", "reason": "project_skill"})
    elif include_skills and pp is not None:
        excluded.append({"path": ".opencode/skills", "kind": "skill_root", "reason": "project_policy:index_skills=false"})
    return docs, included, excluded


def collect_project_documents(paths: HarnessPaths, include_artifacts: bool = False, include_code: bool = False, include_skills: bool = False, project_id: str | None = None, session_id: str | None = None) -> list[rag_backend.SearchDocument]:
    docs, _, _ = _collect_project_documents_with_plan(
        paths,
        include_artifacts=include_artifacts,
        include_code=include_code,
        include_skills=include_skills,
        project_id=project_id,
        session_id=session_id,
    )
    return docs


def collect_global_documents(paths: HarnessPaths, include_skills: bool = True) -> list[rag_backend.SearchDocument]:
    ensure_dirs(paths)
    docs = [doc for r in global_records(paths) if (doc := _record_to_doc(r, "global", None)) is not None]
    if include_skills:
        for skill in search_skills("", scope="global", limit=50, paths=paths):
            docs.append(rag_backend.SearchDocument(
                id=rag_backend.stable_doc_id("global", "skill", skill.get("name", ""), skill.get("path", "")),
                scope="global",
                kind="skill",
                title=f"skill: {skill.get('name')}",
                text=f"{skill.get('name')}\n{skill.get('description')}\n{' '.join(skill.get('tags', []))}",
                source_path=str(skill.get("path", "")),
                project_id=None,
                metadata={k: v for k, v in skill.items() if k != "text"},
            ))
    return docs


def project_fts_db(paths: HarnessPaths, project_id: str | None = None, session_id: str | None = None) -> Path:
    pp = project_workspace_path(paths, project_id=project_id, session_id=session_id)
    if pp is not None:
        return pp.index_dir / "sqlite" / "awoki_project_fts.sqlite"
    return rag_backend.fts_db_path(paths.root, "project")


def global_fts_db(paths: HarnessPaths) -> Path:
    return rag_backend.fts_db_path(paths.global_memory_dir, "global")


def project_index_preview(
    project_id: str | None = None,
    *,
    include_artifacts: bool = True,
    include_code: bool = False,
    include_skills: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
    refresh_views: bool = True,
) -> dict[str, Any]:
    """Return the fail-closed project indexing plan without modifying indexes."""
    paths = paths or HarnessPaths.from_env()
    effective_project_id = project_id or attached_project_id(paths, session_id=session_id or None)
    if not effective_project_id:
        return {
            "status": "rejected",
            "scope": "project",
            "reason": "No project is attached and no project_id was supplied; project indexing never falls back to repository or legacy memory.",
        }
    pp = project_workspace_path(paths, project_id=effective_project_id, session_id=session_id or None)
    if pp is None:
        return {"status": "not_found", "scope": "project", "project_id": effective_project_id}
    # Normal preview and apply evaluate the same generated continuity views.
    # Read-only diagnostics may explicitly skip this projection refresh so they
    # can report drift without repairing or otherwise mutating project state.
    if refresh_views:
        project_workspace.refresh_project_files(paths.root, effective_project_id)
    docs, included, excluded = _collect_project_documents_with_plan(
        paths,
        include_artifacts=include_artifacts,
        include_code=include_code,
        include_skills=include_skills,
        project_id=effective_project_id,
        session_id=session_id or None,
    )
    try:
        meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    project_policy = meta.get("rag") if isinstance(meta.get("rag"), dict) else {}
    return {
        "status": "preview",
        "scope": "project",
        "project_id": effective_project_id,
        "policy": "fail_closed_allowlist",
        "project_policy": project_policy,
        "document_count": len(docs),
        "included": included,
        "excluded": excluded,
        "rules": {
            "security_analysis": "coverage_first_with_best_effort_value_redaction",
            "raw_and_secret_paths": "not_embedded_or_structurally_indexed; repository code search may account for textual matches locally with opaque previews",
            "project_artifacts": "registered_or_safe_summary_only; auth/security vocabulary never causes exclusion",
            "code": "explicit_only; textual repository coverage is independent of parser support and security vocabulary",
            "no_rag_markers": "honored as explicit user-controlled exclusions",
        },
    }


def _stamp_index_entries(
    entries: list[dict[str, Any]],
    *,
    project_id: str,
    indexed_at: str,
    index_generation: int,
    policy: str,
) -> list[dict[str, Any]]:
    stamped: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        item.setdefault("content_hash", rag_backend.stable_doc_id(
            str(item.get("record_id") or ""),
            str(item.get("path") or ""),
            str(item.get("kind") or ""),
            str(item.get("reason") or ""),
        ))
        item.update({
            "project_id": project_id,
            "indexed_at": indexed_at,
            "index_generation": index_generation,
            "policy": policy,
        })
        stamped.append(item)
    return stamped


def _document_set_hash(docs: list[rag_backend.SearchDocument]) -> str:
    digest = hashlib.sha256()
    for doc in sorted(docs, key=lambda item: item.id):
        digest.update(doc.id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(doc.text.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def _project_vector_is_current(pp: project_workspace.ProjectPaths) -> bool:
    manifest = indexing_policy.read_index_manifest(pp.index_manifest)
    qdrant = ((manifest.get("backends") or {}).get("qdrant") or {}) if isinstance(manifest, dict) else {}
    return bool(
        qdrant.get("status") == "indexed"
        and manifest.get("document_set_hash")
        and qdrant.get("document_set_hash") == manifest.get("document_set_hash")
    )


def _global_index_manifest_path(paths: HarnessPaths) -> Path:
    return paths.global_memory_dir / "index-manifest.json"


def _global_vector_is_current(paths: HarnessPaths) -> bool:
    manifest = indexing_policy.read_index_manifest(_global_index_manifest_path(paths))
    qdrant = ((manifest.get("backends") or {}).get("qdrant") or {}) if isinstance(manifest, dict) else {}
    return bool(
        qdrant.get("status") == "indexed"
        and manifest.get("document_set_hash")
        and qdrant.get("document_set_hash") == manifest.get("document_set_hash")
    )


def _ensure_project_exact_index_current(
    paths: HarnessPaths,
    project_id: str,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    pp = project_workspace_path(paths, project_id=project_id, session_id=session_id or None)
    if pp is None:
        return {"status": "not_found", "project_id": project_id}
    status = project_workspace.project_status(paths.root, project_id, session_id=session_id or None)
    if (status.get("index_freshness") or {}).get("fresh"):
        return {"status": "current", "project_id": project_id}
    prior = indexing_policy.read_index_manifest(pp.index_manifest)
    return index_project(
        include_artifacts=bool(prior.get("include_artifacts", False)),
        include_code=bool(prior.get("include_code", False)),
        include_qdrant=False,
        project_id=project_id,
        session_id=session_id,
        paths=paths,
    )


def _index_change_set(prior: dict[str, Any], current: list[dict[str, Any]]) -> dict[str, Any]:
    def key(item: dict[str, Any]) -> str:
        return str(item.get("record_id") or f"{item.get('path', '')}|{item.get('kind', '')}")

    old = {key(item): item for item in prior.get("included", []) if isinstance(item, dict)}
    new = {key(item): item for item in current}
    added = sorted(k for k in new if k not in old)
    deleted = sorted(k for k in old if k not in new)
    changed = sorted(
        k for k in new.keys() & old.keys()
        if str(new[k].get("content_hash") or "") != str(old[k].get("content_hash") or "")
    )
    unchanged = sorted(k for k in new.keys() & old.keys() if k not in changed)
    return {
        "added": added,
        "changed": changed,
        "deleted": deleted,
        "unchanged_count": len(unchanged),
    }


def index_project(include_artifacts: bool = False, include_code: bool = False, include_qdrant: bool = True, project_id: str | None = None, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Rebuild one project's safe indexes from a deterministic final document set.

    The manifest is written provisionally before generated views are refreshed so
    those views can report the generation as fresh. FTS and Qdrant are then built
    exactly once from the final views and source set.
    """
    paths = paths or HarnessPaths.from_env()
    effective_project_id = project_id or attached_project_id(paths, session_id=session_id or None)
    if not effective_project_id:
        return {
            "status": "rejected",
            "scope": "project",
            "reason": "No project is attached and no project_id was supplied; project indexing never falls back to repository or legacy memory.",
        }
    pp = project_workspace_path(paths, project_id=effective_project_id, session_id=session_id or None)
    if pp is None:
        return {"status": "not_found", "scope": "project", "project_id": effective_project_id}

    effective_project_id = pp.project_id
    project_workspace.refresh_project_files(paths.root, effective_project_id)
    docs, included, excluded = _collect_project_documents_with_plan(
        paths,
        include_artifacts=include_artifacts,
        include_code=include_code,
        include_skills=False,
        project_id=effective_project_id,
        session_id=session_id or None,
    )

    prior = indexing_policy.read_index_manifest(pp.index_manifest)
    meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
    workspace_generation = int((meta.get("continuity") or {}).get("workspace_generation") or 0)
    indexed_at = now_ts()
    index_generation = int(prior.get("index_generation") or 0) + 1
    policy = "fail_closed_allowlist"

    def stamped(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _stamp_index_entries(
            entries,
            project_id=effective_project_id,
            indexed_at=indexed_at,
            index_generation=index_generation,
            policy=policy,
        )

    initial_included = stamped(included)
    initial_excluded = stamped(excluded)
    initial_probe = project_workspace.workspace_index_probe(
        pp,
        include_artifacts=include_artifacts,
        include_code=False,
    )
    initial_source_probe = project_workspace.workspace_index_probe(
        pp,
        include_artifacts=include_artifacts,
        include_code=False,
        include_generated=False,
    )
    manifest = {
        "schema_version": 2,
        "project_id": effective_project_id,
        "index_generation": index_generation,
        "workspace_generation": workspace_generation,
        "indexed_at": indexed_at,
        "policy": policy,
        "project_policy_hash": project_workspace.rag_policy_hash(meta),
        "include_artifacts": include_artifacts,
        "include_code": include_code,
        "general_include_code": False,
        "workspace_probe_hash": initial_probe["hash"],
        "workspace_probe_file_count": initial_probe["file_count"],
        "source_probe_hash": initial_source_probe["hash"],
        "source_probe_file_count": initial_source_probe["file_count"],
        "included": initial_included,
        "excluded": initial_excluded,
        "document_count": len(docs),
        "document_set_hash": _document_set_hash(docs),
        "backends": prior.get("backends", {}) if isinstance(prior.get("backends"), dict) else {},
        "change_set": _index_change_set(prior, initial_included),
    }
    indexing_policy.write_index_manifest(pp.index_manifest, manifest)

    # The generated views now see the provisional generation as current. Re-read
    # the final document set and index it once, avoiding duplicate embeddings and
    # transient stale versions of SITUATION/HANDOFF in Qdrant.
    project_workspace.refresh_project_files(paths.root, effective_project_id)
    docs, included, excluded = _collect_project_documents_with_plan(
        paths,
        include_artifacts=include_artifacts,
        include_code=include_code,
        include_skills=False,
        project_id=effective_project_id,
        session_id=session_id or None,
    )
    included = stamped(included)
    excluded = stamped(excluded)
    final_probe = project_workspace.workspace_index_probe(
        pp,
        include_artifacts=include_artifacts,
        include_code=False,
    )
    final_source_probe = project_workspace.workspace_index_probe(
        pp,
        include_artifacts=include_artifacts,
        include_code=False,
        include_generated=False,
    )
    final_document_hash = _document_set_hash(docs)
    manifest.update({
        "workspace_probe_hash": final_probe["hash"],
        "workspace_probe_file_count": final_probe["file_count"],
        "source_probe_hash": final_source_probe["hash"],
        "source_probe_file_count": final_source_probe["file_count"],
        "included": included,
        "excluded": excluded,
        "document_count": len(docs),
        "document_set_hash": final_document_hash,
        "change_set": _index_change_set(prior, included),
    })

    fts = rag_backend.rebuild_fts(
        project_fts_db(paths, project_id=effective_project_id, session_id=session_id or None),
        docs,
        scope="project",
    )
    qdrant = (
        rag_backend.index_qdrant(docs, replace_scope="project", replace_project_id=effective_project_id)
        if include_qdrant
        else {"status": "skipped", "backend": "qdrant", "reason": "include_qdrant=false", "document_count": 0}
    )
    prior_qdrant = ((prior.get("backends") or {}).get("qdrant") or {}) if isinstance(prior, dict) else {}
    if qdrant.get("status") == "indexed":
        qdrant_state = {
            "status": "indexed",
            "document_set_hash": final_document_hash,
            "indexed_at": indexed_at,
            "index_generation": index_generation,
            "workspace_generation": workspace_generation,
        }
    elif not include_qdrant and prior_qdrant.get("document_set_hash") == final_document_hash:
        qdrant_state = dict(prior_qdrant)
    else:
        qdrant_state = {
            "status": "stale" if not include_qdrant else str(qdrant.get("status") or "error"),
            "document_set_hash": str(prior_qdrant.get("document_set_hash") or ""),
            "last_attempt_at": indexed_at,
            "reason": str(qdrant.get("reason") or "semantic index was not rebuilt for the current document set"),
        }
    manifest["backends"] = {
        "fts": {
            "status": str(fts.get("status") or "indexed"),
            "document_set_hash": final_document_hash,
            "indexed_at": indexed_at,
            "index_generation": index_generation,
            "workspace_generation": workspace_generation,
        },
        "qdrant": qdrant_state,
    }
    indexing_policy.write_index_manifest(pp.index_manifest, manifest)
    code_index: dict[str, Any]
    code_allowed = bool(((meta.get("rag") or {}) if isinstance(meta.get("rag"), dict) else {}).get("index_code", False))
    if include_code and code_allowed:
        repositories = project_workspace.project_repositories(paths.root, effective_project_id)
        if len(repositories) <= 1:
            rid = str(repositories[0].get("repo_id") or "") if repositories else ""
            code_index = code_search.index_project_code(
                paths, effective_project_id, include_qdrant=include_qdrant, force=False, repo=rid
            ) if repositories else {"status": "not_found", "reason": "project has no enabled repositories"}
        else:
            repo_results = []
            for repository in repositories:
                rid = str(repository.get("repo_id") or "")
                result = code_search.index_project_code(
                    paths, effective_project_id, include_qdrant=include_qdrant, force=False, repo=rid
                )
                repo_results.append({"repo_id": rid, **result})
            failures = [row for row in repo_results if row.get("status") not in {"indexed", "current"}]
            code_index = {
                "status": "partial" if failures else "indexed",
                "project_id": effective_project_id,
                "multi_repo": True,
                "repositories": repo_results,
            }
    elif include_code:
        code_index = {
            "status": "skipped",
            "reason": "project_policy:index_code=false",
            "project_id": effective_project_id,
        }
    else:
        code_index = {"status": "skipped", "reason": "include_code=false"}
    return {
        "status": "indexed",
        "scope": "project",
        "project_id": effective_project_id,
        "document_count": len(docs),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "fts": fts,
        "qdrant": qdrant,
        "code_index": code_index,
        "manifest": str(pp.index_manifest),
        "index_generation": index_generation,
        "workspace_generation": workspace_generation,
        "change_set": manifest["change_set"],
    }


def index_global(include_qdrant: bool = True, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    docs = collect_global_documents(paths)
    document_hash = _document_set_hash(docs)
    manifest_path = _global_index_manifest_path(paths)
    prior = indexing_policy.read_index_manifest(manifest_path)
    indexed_at = now_ts()
    index_generation = int(prior.get("index_generation") or 0) + 1

    fts = rag_backend.rebuild_fts(global_fts_db(paths), docs, scope="global")
    qdrant = (
        rag_backend.index_qdrant(docs, replace_scope="global", replace_project_id=None)
        if include_qdrant
        else {"status": "skipped", "backend": "qdrant", "reason": "include_qdrant=false", "document_count": 0}
    )
    prior_qdrant = ((prior.get("backends") or {}).get("qdrant") or {}) if isinstance(prior, dict) else {}
    if qdrant.get("status") == "indexed":
        qdrant_state = {
            "status": "indexed",
            "document_set_hash": document_hash,
            "indexed_at": indexed_at,
            "index_generation": index_generation,
        }
    elif not include_qdrant and prior_qdrant.get("document_set_hash") == document_hash:
        qdrant_state = dict(prior_qdrant)
    else:
        qdrant_state = {
            "status": "stale" if not include_qdrant else str(qdrant.get("status") or "error"),
            "document_set_hash": str(prior_qdrant.get("document_set_hash") or ""),
            "last_attempt_at": indexed_at,
            "reason": str(qdrant.get("reason") or "semantic index was not rebuilt for the current document set"),
        }
    manifest = {
        "schema_version": 1,
        "scope": "global",
        "index_generation": index_generation,
        "indexed_at": indexed_at,
        "document_count": len(docs),
        "document_set_hash": document_hash,
        "backends": {
            "fts": {
                "status": str(fts.get("status") or "indexed"),
                "document_set_hash": document_hash,
                "indexed_at": indexed_at,
                "index_generation": index_generation,
            },
            "qdrant": qdrant_state,
        },
    }
    indexing_policy.write_index_manifest(manifest_path, manifest)
    return {
        "status": "indexed",
        "scope": "global",
        "document_count": len(docs),
        "fts": fts,
        "qdrant": qdrant,
        "manifest": str(manifest_path),
        "index_generation": index_generation,
    }


def index_all(include_artifacts: bool = False, include_code: bool = False, include_qdrant: bool = True, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    return {
        "status":"indexed",
        "project": index_project(include_artifacts=include_artifacts, include_code=include_code, include_qdrant=include_qdrant, paths=paths),
        "global": index_global(include_qdrant=include_qdrant, paths=paths),
    }


def _legacy_hits_to_rag(hits: list[dict[str, Any]], backend: str = "jsonl_scan") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in hits:
        text = record_text(h)
        out.append({
            "retrieval_backend": backend,
            "id": rag_backend.stable_doc_id(str(h.get("_source_file", "legacy")), str(h.get("_line", "")), h.get("kind", ""), text[:120]),
            "scope": h.get("scope"),
            "kind": h.get("kind"),
            "project_id": h.get("project_id"),
            "source_path": h.get("_source_file"),
            "line": h.get("_line"),
            "title": h.get("title") or h.get("hypothesis") or h.get("label") or h.get("kind"),
            "preview": text[:1200],
            "score": float(h.get("score", 0.0) or 0.0),
            "metadata": _safe_metadata(h),
        })
    return out


def _generic_global_hits(hits: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude optional-adapter inventories from ordinary reusable-memory recall.

    This is a query-time guard as well as an indexing-policy guard so an older
    Qdrant/global index cannot leak Burp inventory into an unrelated project
    after an upgrade but before a full vector refresh.
    """
    out: list[dict[str, Any]] = []
    for hit in hits:
        kind = str(hit.get("kind") or (hit.get("metadata") or {}).get("kind") or "").lower()
        source = str(hit.get("source_path") or (hit.get("metadata") or {}).get("source_path") or "").lower()
        if kind.startswith("burp_") or kind == "burp_inventory" or "/burp/" in source or source.startswith("burp:"):
            continue
        out.append(hit)
    return out


def search_rag(query: str, scope: str = "project", include_global: bool = False, limit: int = 10, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = attached_project_id(paths, session_id=session_id or None)
    project_warning = ""
    if scope in {"project", "all"} and not project_id:
        project_warning = "No project is attached to this session; project retrieval was skipped without using legacy or repository-name fallback."
    # FTS is cheap enough to refresh memory + skill docs at query time. Artifacts/code are indexed explicitly.
    if os.environ.get("AWOKI_DISABLE_AUTO_FTS") != "1":
        if scope in {"project", "all"} and project_id:
            _ensure_project_exact_index_current(paths, project_id, session_id=session_id)
        if include_global or scope in {"global", "all"}:
            index_global(include_qdrant=False, paths=paths)
    project_hits: list[dict[str, Any]] = []
    global_hits: list[dict[str, Any]] = []
    if scope in {"project", "all"} and project_id:
        project_fts = rag_backend.search_fts(project_fts_db(paths, project_id=project_id, session_id=session_id or None), query, scope="project", limit=limit)
        pp = project_workspace_path(paths, project_id=project_id, session_id=session_id or None)
        project_vec = (
            rag_backend.search_qdrant(query, scope="project", project_id=project_id, limit=limit)
            if pp is not None and _project_vector_is_current(pp)
            else []
        )
        project_legacy = _legacy_hits_to_rag(search_records(query, [r for r in project_records(paths, session_id=session_id or None) if not _is_sensitive_record(r)], limit=limit))
        project_hits = rag_backend.merge_hits(project_fts, project_vec, project_legacy, limit=max(limit, 30))
        project_hits = rag_backend.rerank_hits(query, project_hits, limit=limit)
    if include_global or scope in {"global", "all"}:
        global_fts = _generic_global_hits(rag_backend.search_fts(global_fts_db(paths), query, scope="global", limit=limit))
        global_vec = _generic_global_hits(
            rag_backend.search_qdrant(query, scope="global", project_id=None, limit=limit)
            if _global_vector_is_current(paths)
            else []
        )
        global_legacy = _legacy_hits_to_rag(search_records(query, global_records(paths), limit=limit))
        global_hits = rag_backend.merge_hits(global_fts, global_vec, global_legacy, limit=max(limit, 30))
        global_hits = rag_backend.rerank_hits(query, global_hits, limit=limit)
    return {
        "query": query,
        "scope": scope,
        "include_global": include_global,
        "project_first": True,
        "project_id": project_id,
        "warning": project_warning,
        "project_hits": project_hits,
        "global_hits": global_hits,
        "retrieval": {
            "sqlite_fts": "enabled",
            "qdrant": {
                "project_current": bool(project_id and project_workspace_path(paths, project_id=project_id, session_id=session_id or None) and _project_vector_is_current(project_workspace_path(paths, project_id=project_id, session_id=session_id or None))),
                "global_current": _global_vector_is_current(paths),
                "policy": "used_only_when_document_set_hash_matches",
            },
            "fallback": "jsonl_scan",
            "project_fts_db": str(project_fts_db(paths, project_id=project_id, session_id=session_id or None)) if project_id else None,
            "global_fts_db": str(global_fts_db(paths)),
            "qdrant_collection": rag_backend.qdrant_collection_name(),
            "runtime": rag_backend.retrieval_runtime_status(),
        },
    }


def search_project_memory(query: str, limit: int = 10, include_sensitive: bool = False, session_id: str = "", paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    if not attached_project_id(paths, session_id=session_id or None):
        return []
    records = project_records(paths, session_id=session_id or None)
    if not include_sensitive:
        records = [r for r in records if not _is_sensitive_record(r)]
    return search_records(query, records, limit=limit)


def search_global_memory(query: str, limit: int = 10, include_sensitive: bool = False, paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    return search_records(query, global_records(paths, include_sensitive=include_sensitive), limit=limit)


def project_create(name: str, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_create(paths.root, name, session_id=session_id or None)


def _project_open_prior_material(paths: HarnessPaths, project_id: str, limit: int = 6) -> list[dict[str, Any]]:
    """Return pointers, not duplicated continuity payloads, for normal project-open orientation."""
    pp = project_workspace.paths_for(paths.root, project_id)
    rows: list[dict[str, Any]] = []
    reports = pp.project_dir / "reports"
    if reports.is_dir():
        candidates = []
        for path in reports.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    candidates.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        for _, path in sorted(candidates, key=lambda item: (-item[0], item[1].as_posix()))[:limit]:
            try:
                rel = path.relative_to(pp.project_dir).as_posix()
            except ValueError:
                continue
            rows.append({"kind": "project_report", "path": rel, "reason": "recent_project_report"})
    if len(rows) < limit:
        for item in reversed(project_workspace.continuity_records(pp)):
            if len(rows) >= limit:
                break
            if str(item.get("kind") or "") not in {"finding", "hypothesis", "decision", "direction", "possible_continuation"}:
                continue
            summary = str(item.get("summary") or "").strip()
            record_id = str(item.get("id") or "").strip()
            if not summary or not record_id:
                continue
            rows.append({
                "kind": "continuity_record",
                "record_id": record_id,
                "record_kind": str(item.get("kind") or ""),
                "summary": summary[:320],
                "reason": "recent_meaningful_project_record",
            })
    return rows[:limit]


def _project_open_projection(
    paths: HarnessPaths,
    result: dict[str, Any],
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Slim normal-open projection; project_resume/project_handoff retain the dense views."""
    project_id = str(result.get("project_id") or "")
    if not project_id or result.get("status") not in {"resumed", "created", "already_exists"}:
        return result
    work = work_ledger.status(paths.root, session_id) if session_id else {"status": "none"}
    active_work: dict[str, Any] = {"status": "none", "todos": [], "active_references": []}
    if work.get("status") == "ok" and str(work.get("project_id") or "") in {"", project_id}:
        active_work = {
            "status": "ok",
            "todo_generation": int(work.get("todo_generation") or 0),
            "todos_need_review": bool(work.get("todos_need_review")),
            "todos": list(work.get("todos") or [])[:12],
            "references_need_review": bool(work.get("references_need_review")),
            "active_references": list(work.get("active_references") or [])[:8],
        }
    changes = []
    for row in list(result.get("changes_since_previous_handoff") or [])[:4]:
        if isinstance(row, dict):
            changes.append({
                "id": row.get("id"),
                "kind": row.get("kind"),
                "summary": str(row.get("summary") or "")[:320],
                "timestamp": row.get("timestamp") or row.get("created_at"),
            })
    continuation = {
        "summary": str(result.get("narrative") or "")[:1200],
        "changes_since_previous_handoff": changes,
        "uncertainties": list(result.get("uncertainties") or [])[:6],
        "possible_continuations": list(result.get("possible_continuations") or [])[:4],
        "suggested_next_action": result.get("suggested_next_action") or result.get("next_action"),
        "user_direction_overrides_suggestion": True,
    }
    projected = {
        "status": result.get("status"),
        "project_id": project_id,
        "attached_for_current_session": bool(result.get("attached_for_current_session")),
        "session": result.get("session") or {},
        "active_work": active_work,
        "continuity": continuation,
        "prior_material": _project_open_prior_material(paths, project_id),
        "detail_access": {
            "project_resume": "Return the dense SITUATION/HANDOFF continuity view when explicitly needed.",
            "project_handoff": "Refresh/inspect the detailed handoff projection.",
            "project_search": "Search durable project memory instead of loading all prior material on open.",
        },
        "projection_policy": {
            "normal_open_is_slim": True,
            "full_situation_included": False,
            "full_handoff_included": False,
            "recent_reflections_included": False,
            "important_knowledge_dump_included": False,
            "reason": "J2 showed duplicate continuity projections created context cost without improving the review.",
        },
    }
    # New-project project_status fields remain useful and non-duplicative; retain
    # only a small subset rather than the full status response.
    for key in ("summary", "warnings", "index_freshness"):
        if key in result:
            projected[key] = result[key]
    return projected


def project_open(name: str, create_if_missing: bool = False, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    result = project_workspace.project_open(paths.root, name, create_if_missing=create_if_missing, session_id=session_id or None)
    projected = _project_open_projection(paths, result, session_id=session_id)
    project_id = str(projected.get("project_id") or "")
    if projected.get("status") in {"resumed", "created", "already_exists"} and project_id:
        projected["repository_index_advice"] = _repository_index_advice(paths, project_id)
    return projected


_HARNESS_SELF_CHECKS: dict[str, list[str]] = {
    "compaction_acceptance_boundaries": [
        "test_continuity.AcceptanceRunProgressionTests.test_durable_contract_survives_compaction_with_generation_and_execution_invariants",
        "test_continuity.AcceptanceRunProgressionTests.test_machine_protocol_enforcement_downgrades_native_tool_deviation",
        "test_continuity.AcceptanceRunProgressionTests.test_machine_pass_requirements_downgrade_unsatisfied_pass_to_incomplete",
        "test_continuity.AcceptanceRunProgressionTests.test_current_run_evidence_provenance_is_enforced_without_changing_stable_ev_identity",
        "test_continuity.AcceptanceRunProgressionTests.test_orchestration_provenance_is_separate_and_required_scheduler_calls_can_pass",
        "test_continuity.AcceptanceRunProgressionTests.test_acceptance_control_tools_cannot_self_prove_current_test",
        "test_continuity.AcceptanceRunProgressionTests.test_compaction_history_retains_bounded_event_sequence",
        "test_continuity.AcceptanceRunProgressionTests.test_acceptance_attempt_history_is_immutable_and_referenceable",
        "test_continuity.AcceptanceRunProgressionTests.test_acceptance_record_returns_bounded_prior_attempt_context_without_self_reference",
        "test_continuity.AcceptanceRunProgressionTests.test_interface_invocation_limit_applies_to_awoki_mcp_not_only_native_tools",
        "test_continuity.AcceptanceRunProgressionTests.test_compaction_history_records_auto_vs_explicit_trigger_without_guessing",
        "test_continuity.AcceptanceRunProgressionTests.test_oversized_acceptance_note_rejection_explains_where_rich_context_belongs",
        "test_continuity.AgentRuntimeBoundaryTests.test_reasoning_only_terminal_turn_is_structurally_detected_without_reasoning_content",
        "test_continuity.AgentRuntimeBoundaryTests.test_completed_tool_without_text_followup_is_separate_runtime_anomaly",
        "test_continuity.AcceptanceRunPersistenceTests.test_reranker_diagnostic_selector_is_derived_without_mutating_payload",
    ],
    "reference_navigation_boundaries": [
        "test_continuity.ReferenceCatalogTests.test_human_reference_metadata_keeps_stable_id_authoritative",
        "test_continuity.ReferenceCatalogTests.test_reference_resolution_refuses_ambiguous_natural_language_match",
        "test_continuity.ReferenceCatalogTests.test_candidate_reference_distinguishes_first_materialization_from_later_occurrences",
    ],
    "detached_self_resume_bounds": [
        "test_continuity.DurableContinuationTests.test_auto_resume_claims_are_bounded_and_deadline_is_enforced_at_claim_time",
        "test_continuity.DurableContinuationTests.test_rescheduling_active_continuation_preserves_chain_budget_but_terminal_restart_resets_it",
        "test_continuity.DurableContinuationTests.test_rescheduling_expired_active_chain_does_not_refresh_lifetime",
    ],
}


def harness_self_check(check: str, *, paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Run one bounded, allow-listed hermetic Awoki regression check.

    This is intentionally not a generic command runner. It exists so acceptance
    suites can verify selected harness invariants through MCP without falling back
    to Bash/Read or granting arbitrary repository command execution.
    """
    paths = paths or HarnessPaths.from_env()
    key = str(check or "").strip()
    tests = _HARNESS_SELF_CHECKS.get(key)
    if not tests:
        return {
            "status": "rejected", "reason": "unknown_harness_self_check",
            "available_checks": sorted(_HARNESS_SELF_CHECKS),
        }
    tests_dir = paths.root / ".harness" / "tests"
    if not tests_dir.is_dir():
        return {"status": "blocked", "reason": "harness_tests_unavailable", "check": key}
    env = dict(os.environ)
    pythonpath = [str(paths.root / ".harness"), str(tests_dir)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *tests],
            cwd=tests_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "reason": "harness_self_check_timeout", "check": key, "timeout_seconds": 20}
    output = str(proc.stdout or "")[-8_000:]
    return {
        "status": "passed" if proc.returncode == 0 else "failed",
        "check": key,
        "returncode": int(proc.returncode),
        "test_count": len(tests),
        "tests": list(tests),
        "output": output,
    }


def _repository_index_advice(paths: HarnessPaths, project_id: str) -> dict[str, Any]:
    """Return passive, no-network code/vector readiness guidance for a project.

    Opening or registering a repository must never silently trigger remote
    embedding work.  This helper instead makes the current state and the exact
    explicit refresh action visible to the model/user immediately.
    """
    registry = project_workspace.project_repository_registry(paths.root, project_id)
    repositories = project_workspace.project_repositories(paths.root, project_id)
    if not repositories:
        return {
            "status": "no_repositories",
            "project_id": project_id,
            "action_required": False,
            "message": "No enabled repositories are registered for this project.",
        }

    rows: list[dict[str, Any]] = []
    needs_code = False
    needs_vectors = False
    for repository in repositories:
        repo_id = str(repository.get("repo_id") or "")
        state = code_search.index_status(paths, project_id, repo=repo_id)
        freshness = dict(state.get("freshness") or {})
        lexical_current = bool(freshness.get("lexical_current", False))
        vector_current = bool(freshness.get("vector_current", False))
        needs_code = needs_code or not lexical_current
        needs_vectors = needs_vectors or not vector_current
        rows.append({
            "repo_id": repo_id,
            "path": repository.get("path"),
            "default": bool(repository.get("default")),
            "status": state.get("status"),
            "lexical_current": lexical_current,
            "vector_current": vector_current,
            "vector_status": freshness.get("vector_status"),
            "vector_reason": freshness.get("vector_reason"),
            "repository_assurance": state.get("repository_assurance"),
        })

    if registry.get("mode") == "legacy" and all(row.get("status") == "not_indexed" for row in rows):
        return {
            "status": "not_requested",
            "project_id": project_id,
            "action_required": False,
            "repositories": rows,
            "message": (
                "Code indexing has not been requested for this legacy single-repository project. "
                "The first repository-analysis request can build the local structural index; semantic vector materialization remains explicit."
            ),
            "needs_structural_refresh": True,
            "needs_semantic_refresh": True,
        }

    active_index_jobs = code_index_jobs.active(paths.root, project_id) if needs_code else []
    if active_index_jobs:
        return {
            "status": "structural_refresh_running",
            "project_id": project_id,
            "action_required": False,
            "repositories": rows,
            "active_index_jobs": active_index_jobs,
            "message": "Local structural/FTS indexing is already running in the background. Do not start a duplicate or autonomously poll; check code_index_refresh_status when the user asks or when a later requested action needs fresh state.",
            "recommended_action": {
                "tool": "code_index_refresh_status",
                "arguments": {"name": project_id, "job_id": str(active_index_jobs[0].get("job_id") or "")},
            },
            "needs_structural_refresh": True,
            "needs_semantic_refresh": needs_vectors,
        }

    stale_existing_structural = needs_code and any(str(row.get("status") or "") == "stale" for row in rows)
    if stale_existing_structural:
        active_vector_jobs = code_vector_jobs.active(paths.root, project_id)
        if active_vector_jobs:
            return {
                "status": "semantic_refresh_running",
                "project_id": project_id,
                "action_required": False,
                "repositories": rows,
                "active_vector_jobs": active_vector_jobs,
                "message": "Semantic code-vector materialization is already running and includes local structural indexing as needed. Do not start a duplicate refresh; check code_vector_refresh_status only when requested or required by a later action.",
                "recommended_action": {
                    "tool": "code_vector_refresh_status",
                    "arguments": {"name": project_id, "job_id": str(active_vector_jobs[0].get("job_id") or "")},
                },
                "needs_structural_refresh": True,
                "needs_semantic_refresh": True,
            }
        return {
            "status": "structural_refresh_recommended",
            "project_id": project_id,
            "action_required": True,
            "repositories": rows,
            "message": "An existing local structural/FTS snapshot is stale. Refresh it in the detached local worker first; this performs no remote embedding or Qdrant writes. After it completes, refresh semantic vectors separately only if they remain stale.",
            "recommended_action": {
                "tool": "code_index_refresh_start",
                "arguments": {"name": project_id},
            },
            "refresh_execution": "background",
            "refresh_note": "The start call returns immediately. code_index_refresh_status exposes bounded file/parser progress without source text; never autonomously poll it in a loop.",
            "needs_structural_refresh": True,
            "needs_semantic_refresh": needs_vectors,
        }

    if needs_vectors:
        active_vector_jobs = code_vector_jobs.active(paths.root, project_id)
        if active_vector_jobs:
            return {
                "status": "semantic_refresh_running",
                "project_id": project_id,
                "action_required": False,
                "repositories": rows,
                "active_vector_jobs": active_vector_jobs,
                "message": "Semantic code-vector materialization is already running in the background. Do not autonomously poll it or start a duplicate refresh; check code_vector_refresh_status when the user asks or when a later requested action needs fresh state.",
                "recommended_action": {
                    "tool": "code_vector_refresh_status",
                    "arguments": {"name": project_id, "job_id": str(active_vector_jobs[0].get("job_id") or "")},
                },
                "needs_structural_refresh": needs_code,
                "needs_semantic_refresh": True,
            }
        message = (
            "Structural code indexing and semantic vectors are missing or stale for at least one repository. "
            "Offer to refresh code + Qdrant vectors before repository analysis; do not trigger remote embedding implicitly."
            if needs_code
            else
            "Structural/FTS code search is current, but semantic code vectors are missing or stale for at least one repository. "
            "Conceptual search can fall back locally; offer to refresh Qdrant vectors before semantic-heavy analysis and do not trigger remote embedding implicitly."
        )
        return {
            "status": "semantic_refresh_recommended",
            "project_id": project_id,
            "action_required": True,
            "repositories": rows,
            "message": message,
            "recommended_action": {
                "tool": "code_vector_refresh_start",
                "arguments": {
                    "name": project_id,
                },
            },
            "refresh_execution": "background",
            "refresh_note": "First-time semantic code-vector materialization can be CPU-intensive at the embedding backend. The start call returns control immediately; do not autonomously poll. code_vector_refresh_status exposes bounded chunk/vector/batch progress on demand while MCP remains responsive.",
            "needs_structural_refresh": needs_code,
            "needs_semantic_refresh": True,
        }

    return {
        "status": "ready",
        "project_id": project_id,
        "action_required": False,
        "repositories": rows,
        "message": "Structural code indexes and semantic vectors are current for all enabled repositories.",
        "needs_structural_refresh": False,
        "needs_semantic_refresh": False,
    }


def _resolve_managed_project(paths: HarnessPaths, name: str = "", session_id: str = "") -> tuple[str | None, dict[str, Any] | None]:
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return None, {"status": "rejected", "reason": "No project is attached and no project name was supplied."}
    if not project_workspace.project_exists(paths.root, project_id):
        return None, {"status": "not_found", "project_id": project_id}
    return project_id, None


def project_repo_add(
    repo_id: str,
    path: str = "",
    *,
    name: str = "",
    make_default: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Register one project child repository, inferring repo/<repo_id> by default."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    rel = str(path or f"repo/{project_workspace.clean_project_id(repo_id)}").strip().replace("\\", "/")
    rel_path = Path(rel)
    if (
        rel_path.is_absolute()
        or not rel_path.parts
        or ".." in rel_path.parts
        or rel_path == Path("repo")
        or rel_path.parts[0] != "repo"
    ):
        return {
            "status": "rejected",
            "project_id": project_id,
            "repo_id": repo_id,
            "path": rel,
            "reason": "registered repository path must be a project-relative child under repo/, e.g. repo/oathkeeper",
        }
    rel = rel_path.as_posix()
    pp = project_workspace.paths_for(paths.root, project_id)
    candidate = (pp.project_dir / rel_path).resolve()
    try:
        candidate.relative_to((pp.project_dir / "repo").resolve())
    except ValueError:
        return {
            "status": "rejected",
            "project_id": project_id,
            "repo_id": repo_id,
            "path": rel,
            "reason": "registered repository path must stay under the project repo/ container",
        }
    if not candidate.exists():
        return {
            "status": "not_found",
            "project_id": project_id,
            "repo_id": repo_id,
            "path": rel,
            "reason": "repository path does not exist",
        }
    repository_state = project_workspace.repository_root_status(candidate)
    if repository_state.get("git") and repository_state.get("invalid_repo_root"):
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_id": repo_id,
            "path": rel,
            "reason": "registered repository path must be the exact Git top-level",
            "repository": repository_state,
        }
    registered = project_workspace.project_repo_add(paths.root, project_id, repo_id, rel, default=make_default)
    registered["repository"] = repository_state
    registered["repository_index_advice"] = _repository_index_advice(paths, project_id)
    return registered


def project_repo_list(name: str = "", session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    rows = project_workspace.project_repositories(paths.root, project_id, enabled_only=False)
    return {
        "status": "ok",
        "project_id": project_id,
        "registry": project_workspace.project_repository_registry(paths.root, project_id),
        "repositories": [{k: v for k, v in row.items() if k != "root"} for row in rows],
        "repository_index_advice": _repository_index_advice(paths, project_id),
    }


def project_repo_remove(repo_id: str, *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    result = project_workspace.project_repo_remove(paths.root, project_id, repo_id)
    result["repository_index_advice"] = _repository_index_advice(paths, project_id)
    return result


def project_repo_default(repo_id: str, *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return project_workspace.project_repo_default(paths.root, project_id, repo_id)


def project_source_add(
    source_id: str,
    path: str = "",
    *,
    source_type: str = "directory",
    name: str = "",
    make_default: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Register a deterministic non-Git evidence corpus under project sources/."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    sid = project_workspace.clean_project_id(source_id)
    rel = str(path or f"sources/{sid}").strip().replace("\\", "/")
    try:
        result = project_workspace.project_source_add(
            paths.root,
            project_id,
            sid,
            rel,
            source_type=source_type,
            default=make_default,
        )
    except ValueError as exc:
        return {
            "status": "rejected",
            "project_id": project_id,
            "source_id": sid,
            "path": rel,
            "reason": str(exc),
        }
    result["code_index_status"] = code_search.index_status(paths, project_id, source=sid)
    result["index_note"] = (
        "The corpus is registered but not uploaded or executed. The first codebase_search may materialize only the local structural/FTS index; remote semantic vector refresh remains explicit."
    )
    return result


def project_source_list(
    name: str = "", session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """List generic evidence sources; registered Git repositories appear as type=git."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    rows = project_workspace.project_sources(paths.root, project_id, enabled_only=False)
    return {
        "status": "ok",
        "project_id": project_id,
        "registry": project_workspace.project_source_registry(paths.root, project_id),
        "sources": [{k: v for k, v in row.items() if k != "root"} for row in rows],
    }


def project_source_remove(
    source_id: str, *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    try:
        return project_workspace.project_source_remove(paths.root, project_id, source_id)
    except ValueError as exc:
        return {"status": "rejected", "project_id": project_id, "source_id": source_id, "reason": str(exc)}


def project_source_default(
    source_id: str, *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return project_workspace.project_source_default(paths.root, project_id, source_id)


def project_resume(name: str, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_resume(paths.root, name, session_id=session_id or None)


def _redact_nested(value: Any) -> tuple[Any, bool]:
    return safety.redact_nested(value)


def _redact_analysis_nested(value: Any) -> tuple[Any, bool]:
    return safety.redact_analysis_nested(value)


def _normalized_memory_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_./:-]+", (value or "").lower()))


def _memory_negation_signature(value: str) -> frozenset[str]:
    tokens = set(_normalized_memory_text(value).split())
    markers = {
        "no", "not", "never", "none", "without", "cannot", "can't", "doesn't",
        "doesnt", "isn't", "isnt", "aren't", "arent", "won't", "wont", "disabled",
        "absent", "false", "incorrect", "wrong",
    }
    return frozenset(tokens & markers)


def _capture_reconciliation(
    pp: project_workspace.ProjectPaths,
    summary: str,
    details: str,
    kind: str,
    supersedes: list[str],
) -> dict[str, Any]:
    """Compare a proposed capture with current project memory without rewriting history."""
    proposed = _normalized_memory_text(f"{summary} {details}")
    if not proposed:
        return {"classification": "new", "matches": []}
    matches: list[dict[str, Any]] = []
    for record in reversed(project_workspace.continuity_records(pp)):
        if _is_sensitive_record(record):
            continue
        existing = _normalized_memory_text(f"{record.get('summary', '')} {record.get('details', '')}")
        if not existing:
            continue
        left, right = set(proposed.split()), set(existing.split())
        jaccard = len(left & right) / max(1, len(left | right))
        sequence = SequenceMatcher(None, proposed, existing).ratio()
        score = max(jaccard, sequence)
        if score >= 0.45:
            matches.append({
                "id": str(record.get("id") or ""),
                "kind": str(record.get("kind") or ""),
                "summary": str(record.get("summary") or "")[:300],
                "score": round(score, 4),
                "normalized_text": existing,
                "sources": list(record.get("sources") or []),
                "negation": sorted(_memory_negation_signature(existing)),
            })
    semantic_checked = False
    if _project_vector_is_current(pp):
        semantic_checked = True
        for hit in rag_backend.search_qdrant(
            summary,
            scope="project",
            project_id=pp.project_id,
            memory_only=True,
            limit=5,
        ):
            metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
            record_id = str(metadata.get("id") or hit.get("id") or "")
            if not record_id:
                continue
            score = float(hit.get("score") or 0.0)
            existing = next((row for row in matches if row["id"] == record_id), None)
            if existing:
                existing["semantic_score"] = round(score, 4)
                existing["score"] = round(max(float(existing["score"]), score), 4)
            else:
                matches.append({
                    "id": record_id,
                    "kind": str(hit.get("kind") or metadata.get("kind") or ""),
                    "summary": str(hit.get("title") or hit.get("preview") or "")[:300],
                    "score": round(score, 4),
                    "semantic_score": round(score, 4),
                    "normalized_text": "",
                    "sources": [],
                    "negation": [],
                })
    matches.sort(key=lambda row: row["score"], reverse=True)
    matches = matches[:5]
    proposed_negation = _memory_negation_signature(proposed)
    top = matches[0] if matches else {}
    top_negation = frozenset(top.get("negation") or [])
    negation_mismatch = bool(
        top
        and float(top.get("score") or 0.0) >= 0.65
        and proposed_negation != top_negation
        and (proposed_negation or top_negation)
    )
    if supersedes or kind == "correction":
        classification = "correction"
    elif negation_mismatch:
        classification = "possible_contradiction"
    elif top and top.get("normalized_text") == proposed:
        classification = "exact_restatement"
    elif matches and matches[0]["score"] >= 0.96:
        classification = "duplicate_or_restatement"
    elif matches and matches[0]["score"] >= 0.78:
        classification = "refinement_or_reinforcement"
    elif matches:
        classification = "related"
    else:
        classification = "new"
    return {
        "classification": classification,
        "matches": matches,
        "semantic_checked": semantic_checked,
        "requires_user_review": classification == "possible_contradiction",
    }


def project_capture(
    summary: str,
    *,
    name: str = "",
    details: str = "",
    kind: str = "observation",
    sources: list[Any] | None = None,
    confidence: str = "medium",
    sensitivity: str = "project",
    index_policy: str = "safe",
    tags: list[str] | None = None,
    uncertainty: list[str] | None = None,
    likely_continuation: str = "",
    supersedes: list[str] | None = None,
    state: str = "",
    metadata: dict[str, Any] | None = None,
    allow_sensitive_plaintext: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return {
            "status": "rejected",
            "reason": "No project is attached. Use project_open/project_resume or pass name explicitly; Awoki will not fall back to legacy project memory.",
        }
    pp = project_workspace_path(paths, project_id=project_id, session_id=session_id or None)
    if pp is None:
        return {"status": "not_found", "project_id": project_id}
    if allow_sensitive_plaintext:
        safe_summary, safe_details = summary, details
        safe_sources, safe_uncertainty = sources or [], uncertainty or []
        safe_continuation = likely_continuation
        changed = False
        effective_sensitivity = "secret"
        effective_policy = "no_rag"
    else:
        safe_summary, summary_changed = redact_analysis_text(summary)
        safe_details, details_changed = redact_analysis_text(details)
        safe_sources, sources_changed = _redact_analysis_nested(sources or [])
        safe_uncertainty, uncertainty_changed = _redact_analysis_nested(uncertainty or [])
        safe_continuation, continuation_changed = redact_analysis_text(likely_continuation)
        changed = summary_changed or details_changed or sources_changed or uncertainty_changed or continuation_changed
        # Redaction of an actual value does not make the surrounding security
        # analysis non-retrievable. Explicit sensitivity/no_rag remains honored.
        effective_sensitivity = sensitivity
        effective_policy = index_policy
    reconciliation = {"classification": "sensitive_explicit", "matches": []} if allow_sensitive_plaintext else _capture_reconciliation(pp, safe_summary, safe_details, kind, supersedes or [])
    classification = str(reconciliation.get("classification") or "new")
    matches = reconciliation.get("matches") or []
    top_match = matches[0] if matches else {}
    if classification == "possible_contradiction" and kind != "correction" and not (supersedes or []):
        return {
            "status": "needs_review",
            "project_id": project_id,
            "reason": "The proposed memory may contradict a similar existing record. Review the match, then capture as a correction with supersedes when appropriate.",
            "reconciliation": reconciliation,
        }
    if classification == "exact_restatement" and top_match.get("id"):
        proposed_sources = continuity.normalize_sources(safe_sources)
        existing_sources = continuity.normalize_sources(top_match.get("sources") or [])
        new_sources = [source for source in proposed_sources if source not in existing_sources]
        if not new_sources:
            return {
                "status": "duplicate",
                "project_id": project_id,
                "id": top_match.get("id"),
                "summary": top_match.get("summary"),
                "reconciliation": reconciliation,
                "_write_status": "duplicate_skipped",
            }
        reconciliation["classification"] = "reinforcement"
        reconciliation["reinforces"] = top_match.get("id")
    effective_supersedes = list(supersedes or [])
    if kind == "correction" and not effective_supersedes and reconciliation.get("matches"):
        candidate_id = str(reconciliation["matches"][0].get("id") or "")
        if candidate_id.startswith("cont_"):
            effective_supersedes = [candidate_id]
            reconciliation["auto_supersedes"] = candidate_id
    merged_metadata = dict(metadata or {})
    merged_metadata["memory_reconciliation"] = reconciliation
    if reconciliation.get("reinforces"):
        merged_metadata["reinforces"] = reconciliation["reinforces"]
    elif reconciliation.get("matches") and reconciliation.get("classification") in {"refinement_or_reinforcement", "related"}:
        merged_metadata["relates_to"] = reconciliation["matches"][0].get("id")
    saved = project_workspace.project_capture(
        paths.root,
        project_id,
        safe_summary,
        details=safe_details,
        kind=kind,
        sources=safe_sources,
        confidence=confidence,
        sensitivity=effective_sensitivity,
        index_policy=effective_policy,
        tags=tags or [],
        uncertainty=safe_uncertainty,
        likely_continuation=safe_continuation,
        supersedes=effective_supersedes,
        state=state,
        metadata=merged_metadata,
        allow_sensitive_plaintext=allow_sensitive_plaintext,
    )
    return {"status": "captured" if saved.get("_write_status") == "appended" else "duplicate", "project_id": project_id, "reconciliation": reconciliation, **saved}


def project_status(name: str = "", session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    status = project_workspace.project_status(paths.root, name, session_id=session_id or None)
    if status.get("status") == "ok":
        status["retrieval"] = {
            "sqlite_fts": "enabled",
            "qdrant_url": rag_backend.qdrant_url(),
            "qdrant_collection": rag_backend.qdrant_collection_name(),
            "runtime": rag_backend.retrieval_runtime_status(),
            "failure_policy": "continuity remains usable; vector indexing/search reports or falls back without fabricating hits",
        }
        if bool((status.get("index_policy") or {}).get("index_code")):
            project_id = str(status.get("project_id") or "")
            repositories = project_workspace.project_repositories(paths.root, project_id)
            if len(repositories) > 1:
                rows = []
                for repo_row in repositories:
                    repo_id = str(repo_row.get("repo_id") or "")
                    code_state = code_search.index_status(paths, project_id, deep_verify=False, repo=repo_id)
                    repository = code_state.get("indexed_repository_evidence") or code_state.get("repository_evidence") or {}
                    rows.append({
                        "repo_id": repo_id,
                        "status": code_state.get("status"),
                        "assurance": code_state.get("repository_assurance", repository.get("assurance", "")),
                        "head_sha": repository.get("head_sha", ""),
                        "tree_sha": repository.get("raw_tree_sha", ""),
                        "anomalies": list(repository.get("anomalies") or [])[:12],
                        "lexical_current": bool((code_state.get("freshness") or {}).get("lexical_current", code_state.get("lexical_current", False))),
                    })
                status["code_repository"] = {
                    "status": "ok" if all(row.get("status") in {"ok", "current", "indexed"} for row in rows) else "partial",
                    "multi_repo": True,
                    "repositories": rows,
                    "verification": "passive; use code_index_verify with repo= for deep repository/source verification",
                }
            else:
                code_state = code_search.index_status(paths, project_id, deep_verify=False)
                repository = code_state.get("indexed_repository_evidence") or code_state.get("repository_evidence") or {}
                status["code_repository"] = {
                    "status": code_state.get("status"),
                    "assurance": code_state.get("repository_assurance", repository.get("assurance", "")),
                    "head_sha": repository.get("head_sha", ""),
                    "tree_sha": repository.get("raw_tree_sha", ""),
                    "anomalies": list(repository.get("anomalies") or [])[:12],
                    "lexical_current": bool((code_state.get("freshness") or {}).get("lexical_current", code_state.get("lexical_current", False))),
                    "verification": "passive; use code_index_verify for deep repository/source verification",
                }
    return status


def project_handoff(name: str, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_handoff(paths.root, name)


def project_migrate(name: str, apply: bool = False, paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Preview or non-destructively import legacy typed JSONL into continuity.jsonl."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return continuity_migration.migrate(paths.root, name, apply=apply)


def project_list(paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_list(paths.root)


def project_note(name: str, text: str, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_note(paths.root, name, text)


def project_pending(name: str, title: str, next_action: str, reason: str = "", related_files: list[str] | None = None, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_pending(paths.root, name, title, next_action, reason=reason, related_files=related_files)


def project_mark_pending(name: str, pending_id: str = "", status: str = "done", note: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return project_workspace.project_mark_pending(paths.root, name, pending_id=pending_id, status=status, note=note)


def code_index_refresh_start(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    force: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Start detached local structural/FTS indexing without blocking MCP."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_index_jobs.start(paths.root, project_id, repo=repo, source_id=source_id, force=force)


def code_index_refresh_status(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    job_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Check a detached local structural/FTS indexing job."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_index_jobs.status(paths.root, project_id, job_id=job_id, repo=repo, source_id=source_id)


def code_index_refresh_cancel(
    job_id: str,
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Cancel a detached local structural/FTS indexing job."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_index_jobs.cancel(paths.root, project_id, job_id=job_id)


def code_vector_refresh_start(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Start detached code-vector materialization without blocking the MCP server."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_vector_jobs.start(paths.root, project_id, repo=repo, source_id=source_id)


def code_vector_refresh_status(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    job_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Check a detached code-vector materialization job and return bounded real progress."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_vector_jobs.status(paths.root, project_id, job_id=job_id, repo=repo, source_id=source_id)


def code_vector_refresh_cancel(
    job_id: str,
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Cancel a detached code-vector materialization job."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_vector_jobs.cancel(paths.root, project_id, job_id=job_id)


def repository_prepare_start(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    mode: str = "full",
    resume_goal: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Start one durable parent job that owns repository readiness end-to-end."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        if not name and error.get("status") == "rejected":
            return {
                "status": "managed_scope_required",
                "outcome": "MANAGED_SCOPE_REQUIRED",
                "reason": "Repository preparation requires an existing managed project or an explicitly supplied project name; ad-hoc paths are not silently promoted.",
            }
        return error
    assert project_id is not None
    return repository_prepare_jobs.start(
        paths.root, project_id, repo=repo, source_id=source_id, mode=mode,
        resume_goal=resume_goal, origin_session_id=session_id,
    )


def repository_prepare_status(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    mode: str = "full",
    job_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Return bounded progress for a durable repository preparation parent job."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return repository_prepare_jobs.status(
        paths.root, project_id, job_id=job_id, repo=repo, source_id=source_id, mode=mode,
    )


def repository_prepare_cancel(
    job_id: str,
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Cancel a repository preparation parent job and its currently owned child worker."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return repository_prepare_jobs.cancel(paths.root, project_id, job_id=job_id)


def project_refresh(
    name: str = "",
    *,
    reason: str = "",
    include_artifacts: bool = True,
    include_code: bool = False,
    include_qdrant: bool = False,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return {"status": "rejected", "reason": "No project is attached and no project name was supplied."}
    if project_workspace_path(paths, project_id=project_id) is None:
        return {"status": "not_found", "project_id": project_id}
    views = project_workspace.refresh_project_files(paths.root, project_id)
    indexed = index_project(
        include_artifacts=include_artifacts,
        include_code=include_code,
        include_qdrant=include_qdrant,
        project_id=project_id,
        paths=paths,
    )
    return {"status": "refreshed", "project_id": project_id, "reason": reason, "views": views, "index": indexed}


def project_search(
    query: str,
    *,
    name: str = "",
    include_global: bool = False,
    limit: int = 10,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return {"status": "rejected", "reason": "No project is attached and no project name was supplied."}
    pp = project_workspace_path(paths, project_id=project_id, session_id=session_id or None)
    if pp is None:
        return {"status": "not_found", "project_id": project_id}
    index_refresh = _ensure_project_exact_index_current(paths, project_id, session_id=session_id)
    project_fts = rag_backend.search_fts(project_fts_db(paths, project_id=project_id), query, scope="project", limit=limit)
    project_vec = (
        rag_backend.search_qdrant(query, scope="project", project_id=project_id, limit=limit)
        if _project_vector_is_current(pp)
        else []
    )
    project_fallback = _legacy_hits_to_rag(search_records(query, project_workspace.view_safe_records(project_workspace.continuity_records(pp)), limit=limit))
    project_hits = rag_backend.merge_hits(project_fts, project_vec, project_fallback, limit=max(limit, 30))
    project_hits = rag_backend.rerank_hits(query, project_hits, limit=limit)
    for hit in project_hits:
        hit["source_scope"] = "project"
    global_hits: list[dict[str, Any]] = []
    if include_global:
        index_global(include_qdrant=False, paths=paths)
        global_fts = _generic_global_hits(rag_backend.search_fts(global_fts_db(paths), query, scope="global", limit=limit))
        global_vec = _generic_global_hits(
            rag_backend.search_qdrant(query, scope="global", project_id=None, limit=limit)
            if _global_vector_is_current(paths)
            else []
        )
        global_fallback = _legacy_hits_to_rag(search_records(query, global_records(paths), limit=limit))
        global_hits = rag_backend.merge_hits(global_fts, global_vec, global_fallback, limit=max(limit, 30))
        global_hits = rag_backend.rerank_hits(query, global_hits, limit=limit)
        for hit in global_hits:
            hit["source_scope"] = "global_reusable_knowledge"
    return {
        "status": "ok",
        "query": query,
        "project_id": project_id,
        "project_hits": project_hits,
        "global_hits": global_hits,
        "index_refresh": index_refresh,
        "retrieval": {
            "sqlite_fts": True,
            "qdrant": {
                "project_current": _project_vector_is_current(pp),
                "global_current": _global_vector_is_current(paths),
                "policy": "used_only_when_document_set_hash_matches",
            },
            "jsonl_fallback": True,
            "runtime": rag_backend.retrieval_runtime_status(),
        },
        "rules": [
            "Project hits are authoritative only to the extent supported by their sources.",
            "Global hits are reusable knowledge, not project-established facts.",
            "Raw artifacts are not loaded automatically.",
        ],
    }


def _resolve_code_project(
    paths: HarnessPaths,
    name: str = "",
    session_id: str = "",
) -> tuple[str | None, dict[str, Any] | None]:
    project_id = (
        project_workspace.clean_project_id(name)
        if name
        else attached_project_id(paths, session_id=session_id or None)
    )
    if not project_id:
        return None, {
            "status": "rejected",
            "reason": "No project is attached and no project name was supplied.",
        }
    if project_workspace_path(paths, project_id=project_id, session_id=session_id or None) is None:
        return None, {"status": "not_found", "project_id": project_id}
    return project_id, None



_DIAGNOSTIC_TRACE_ID_RE = re.compile(r"^diag_[0-9a-f]{24}$")
_DIAGNOSTIC_TRACE_CACHE: dict[str, dict[str, Any]] = {}
_DIAGNOSTIC_TRACE_LOCK = threading.Lock()


def _diagnostic_trace_ttl_seconds() -> int:
    try:
        value = int(os.environ.get("AWOKI_CODE_DIAGNOSTIC_TRACE_TTL_SECONDS", "21600"))
    except ValueError:
        value = 21600
    return max(300, min(value, 7 * 24 * 60 * 60))


def _diagnostic_trace_max_entries() -> int:
    try:
        value = int(os.environ.get("AWOKI_CODE_DIAGNOSTIC_TRACE_MAX_ENTRIES", "32"))
    except ValueError:
        value = 32
    return max(4, min(value, 256))


def _cleanup_diagnostic_traces_locked(now: float) -> None:
    expired = [
        trace_id for trace_id, record in _DIAGNOSTIC_TRACE_CACHE.items()
        if float(record.get("expires_epoch") or 0) <= now
    ]
    for trace_id in expired:
        _DIAGNOSTIC_TRACE_CACHE.pop(trace_id, None)
    keep = _diagnostic_trace_max_entries()
    if len(_DIAGNOSTIC_TRACE_CACHE) <= keep:
        return
    ordered = sorted(
        _DIAGNOSTIC_TRACE_CACHE.items(),
        key=lambda item: float(item[1].get("created_epoch") or 0),
        reverse=True,
    )
    for trace_id, _record in ordered[keep:]:
        _DIAGNOSTIC_TRACE_CACHE.pop(trace_id, None)


def _diagnostic_trace_id(project_id: str, query: str) -> str:
    material = f"{time.time_ns()}:{os.getpid()}:{project_id}:{query}".encode("utf-8", errors="replace")
    return "diag_" + hashlib.sha256(material).hexdigest()[:24]


def _store_diagnostic_trace(
    project_id: str,
    query: str,
    result: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    trace_id = _diagnostic_trace_id(project_id, query)
    created_epoch = time.time()
    ttl = _diagnostic_trace_ttl_seconds()
    expires_epoch = created_epoch + ttl
    record = {
        "version": 1,
        "trace_id": trace_id,
        "project_id": project_id,
        "created_at": now_ts(),
        "created_epoch": created_epoch,
        "expires_epoch": expires_epoch,
        "query": query,
        "scope": result.get("scope") or result.get("branch") or {},
        "routing": result.get("routing") or {},
        "candidate_trace": trace,
    }
    with _DIAGNOSTIC_TRACE_LOCK:
        _cleanup_diagnostic_traces_locked(created_epoch)
        _DIAGNOSTIC_TRACE_CACHE[trace_id] = record
        _cleanup_diagnostic_traces_locked(created_epoch)
    return {
        "trace_id": trace_id,
        "stored": True,
        "storage": "mcp_process_memory",
        "lifetime": "current_awoki_mcp_process",
        "rows_inline": 0,
        "pool_size": int(trace.get("pool_size") or 0),
        "retrieval_tool": "code_diagnostics_trace",
        "max_page_size": 50,
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_epoch)),
    }


def _finalize_code_diagnostics(
    paths: HarnessPaths,
    project_id: str,
    query: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    trace = result.pop("_diagnostic_trace", None)
    if not isinstance(trace, dict):
        return result
    details = result.get("details")
    descriptor = details.get("candidate_trace") if isinstance(details, dict) else None
    try:
        metadata = _store_diagnostic_trace(project_id, query, result, trace)
    except Exception as exc:
        metadata = {
            "stored": False,
            "storage": "mcp_process_memory",
            "rows_inline": 0,
            "pool_size": int(trace.get("pool_size") or 0),
            "retrieval_tool": "code_diagnostics_trace",
            "storage_error": f"{type(exc).__name__}: {exc}",
        }
    if isinstance(descriptor, dict):
        descriptor.update(metadata)
    return result


def _read_diagnostic_trace(trace_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not _DIAGNOSTIC_TRACE_ID_RE.fullmatch(trace_id or ""):
        return None, {"status": "rejected", "reason": "invalid diagnostic trace id"}
    now = time.time()
    with _DIAGNOSTIC_TRACE_LOCK:
        _cleanup_diagnostic_traces_locked(now)
        record = _DIAGNOSTIC_TRACE_CACHE.get(trace_id)
        if record is None:
            return None, {"status": "not_found", "trace_id": trace_id}
        # The cached value contains only immutable-by-convention JSON-like
        # diagnostic metadata. Returning a shallow copy prevents top-level
        # mutation by callers without duplicating the bounded trace rows.
        return dict(record), None


def code_diagnostics_trace(
    trace_id: str,
    *,
    name: str = "",
    offset: int = 0,
    limit: int = 25,
    target: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Read a bounded page or target from a process-local metadata-only diagnostic trace."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    record, read_error = _read_diagnostic_trace(trace_id)
    if read_error:
        read_error.setdefault("project_id", project_id)
        return read_error
    assert record is not None
    if str(record.get("project_id") or "") != project_id:
        return {
            "status": "rejected",
            "project_id": project_id,
            "trace_id": trace_id,
            "reason": "diagnostic trace belongs to a different project",
        }
    trace = record.get("candidate_trace") or {}
    columns = list(trace.get("columns") or [])
    rows = list(trace.get("rows") or [])
    selected_rows = rows
    matched_total: int | None = None
    target_value = str(target or "").strip()
    if target_value:
        needle = target_value.casefold()
        try:
            path_index = columns.index("path")
            symbol_index = columns.index("symbol")
        except ValueError:
            return {
                "status": "error",
                "project_id": project_id,
                "trace_id": trace_id,
                "reason": "stored diagnostic trace is missing targetable columns",
            }
        exact: list[list[Any]] = []
        partial: list[list[Any]] = []
        for row in rows:
            values = [str(row[path_index] or ""), str(row[symbol_index] or "")]
            folded = [value.casefold() for value in values if value]
            if any(needle == value for value in folded):
                exact.append(row)
            elif any(needle in value for value in folded):
                partial.append(row)
        selected_rows = exact + partial
        matched_total = len(selected_rows)
    start = max(0, int(offset or 0))
    page_limit = max(1, min(int(limit or 25), 50))
    page = selected_rows[start:start + page_limit]
    return {
        "status": "ok",
        "project_id": project_id,
        "trace_id": trace_id,
        "storage": "mcp_process_memory",
        "lifetime": "current_awoki_mcp_process",
        "created_at": record.get("created_at"),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(record.get("expires_epoch") or 0))) if record.get("expires_epoch") else None,
        "query": record.get("query"),
        "scope": record.get("scope") or {},
        "encoding": "columns+rows",
        "pool_size": int(trace.get("pool_size") or len(rows)),
        "columns": columns,
        "legends": trace.get("legends") or {},
        "target": target_value or None,
        "matched_total": matched_total,
        "offset": start,
        "limit": page_limit,
        "returned": len(page),
        "has_more": start + len(page) < len(selected_rows),
        "rows": page,
    }

def _capture_code_search_evidence(
    paths: HarnessPaths,
    project_id: str,
    result: dict[str, Any],
    *,
    repo: str = "",
    source_id: str = "",
    acceptance_run_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    scope = acceptance_runs.scope_snapshot(paths.root, project_id, repo=repo, source_id=source_id)
    if scope.get("status") != "ok":
        enriched = dict(result)
        enriched["evidence_capture"] = {
            "status": "rejected",
            "reason": "exact managed source identity is required for durable evidence capture",
            "scope": scope,
        }
        return enriched
    current_identity = acceptance_runs.scope_identity(scope)
    if acceptance_run_id:
        run = acceptance_runs.status(paths.root, run_id=acceptance_run_id, project_id=project_id, session_id=session_id)
        run_identity = acceptance_runs.scope_identity(dict(run.get("scope") or {})) if run.get("status") == "ok" else {}
        if run.get("status") != "ok" or run.get("run_status") != "running" or run_identity != current_identity:
            enriched = dict(result)
            enriched["evidence_capture"] = {
                "status": "rejected",
                "reason": "acceptance_run_scope_unavailable_or_drifted",
                "acceptance_run_id": acceptance_run_id,
                "run_status": run.get("run_status") if isinstance(run, dict) else None,
                "expected_scope_identity": run_identity or None,
                "current_scope_identity": current_identity,
            }
            return enriched
    evidence_payload = dict(result)
    # Diagnostics normally keep the complete 100-candidate metadata trace only in
    # MCP-process memory. When evidence capture is explicitly requested, preserve
    # that metadata-only trace inside the project-local raw evidence artifact too,
    # so compaction/MCP restart does not make prior diagnostic support unrecoverable.
    details = result.get("details") if isinstance(result, dict) else None
    descriptor = details.get("candidate_trace") if isinstance(details, dict) else None
    trace_id = str((descriptor or {}).get("trace_id") or "") if isinstance(descriptor, dict) else ""
    if trace_id:
        trace_record, trace_error = _read_diagnostic_trace(trace_id)
        if trace_record is not None and str(trace_record.get("project_id") or "") == project_id:
            evidence_payload["_captured_diagnostic_trace"] = {
                "encoding": "columns+rows",
                "query": trace_record.get("query"),
                "scope": trace_record.get("scope") or {},
                "routing": trace_record.get("routing") or {},
                "candidate_trace": trace_record.get("candidate_trace") or {},
            }
        elif trace_error:
            evidence_payload["_captured_diagnostic_trace_status"] = {
                "status": "unavailable",
                "reason": str(trace_error.get("reason") or trace_error.get("status") or "trace unavailable")[:240],
            }
    stored = evidence_store.put(
        paths.root,
        project_id,
        kind="code_search_result",
        tool="codebase_search",
        payload=evidence_payload,
        scope_identity=current_identity,
        run_id=acceptance_run_id,
        session_id=session_id,
    )
    enriched = dict(result)
    enriched["evidence_capture"] = stored
    return enriched


def codebase_search(
    query: str,
    *,
    name: str = "",
    limit: int = 10,
    refresh_index: bool = False,
    mode: str = "auto",
    view: str = "context",
    use_fts: bool = True,
    use_qdrant: bool = True,
    use_reranker: bool = True,
    result_focus: str = "auto",
    structural_promotion: bool = True,
    strict_backends: bool = False,
    max_chars: int = 0,
    repo: str = "",
    source_id: str = "",
    diagnostic_targets: list[str] | None = None,
    capture_evidence: bool = False,
    acceptance_run_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Search eligible registered evidence sources through Awoki's structural index.

    Natural-language questions are routed deterministically to lexical,
    conceptual, exact, definition, call-graph, or path operations. Conceptual
    discovery can combine FTS + current Qdrant vectors + an optional reranker,
    then apply bounded verified structural candidate expansion, authority-aware
    ranking, and diversity. Tests/config remain searchable; production is only
    preferred when query intent asks for implementation/runtime behavior.
    Diagnostic controls can isolate FTS/Qdrant/reranking and ``strict_backends``
    fails closed when an explicitly required semantic backend is unavailable.
    With ``capture_evidence=true`` the exact returned result, plus the metadata-only
    deep diagnostic trace when one exists, is persisted as a content-addressed
    project-local non-RAG evidence artifact and returned by stable ``evidence_ref``.
    Project continuity and general RAG are never mixed into repository results.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    # /codebase is explicit consent to enable safe source indexing for this project.
    project_workspace.enable_code_index(paths.root, project_id)
    if repo and source_id and repo != source_id:
        return {
            "status": "rejected",
            "project_id": project_id,
            "reason": "repo= and source_id= identify different analysis scopes; provide only one",
        }
    if refresh_index:
        refresh = code_index_jobs.start(
            paths.root, project_id, repo=repo, source_id=source_id, force=True
        )
        return {
            "status": "refresh_started" if refresh.get("status") == "started" else refresh.get("status"),
            "project_id": project_id,
            "query": query,
            "reason": (
                "refresh_index=true now runs as a detached local index job so a full repository parse cannot exceed the MCP request deadline; rerun the search after the job completes"
            ),
            "refresh": refresh,
            "recommended_action": {
                "tool": "code_index_refresh_status",
                "arguments": {"name": project_id, "job_id": str((refresh.get("job") or {}).get("job_id") or "")},
            } if (refresh.get("job") or {}).get("job_id") else None,
        }
    sources = project_workspace.project_sources(paths.root, project_id)
    if capture_evidence and not (repo or source_id) and len(sources) != 1:
        return {
            "status": "rejected",
            "project_id": project_id,
            "query": query,
            "reason": "capture_evidence requires an explicit repo= or source_id= when the project has multiple evidence sources",
        }
    selected_git_repo = repo
    if not selected_git_repo and not source_id and len(sources) == 1 and str(sources[0].get("source_type") or "git") == "git":
        selected_git_repo = str(sources[0].get("repo_id") or sources[0].get("source_id") or "")
    if selected_git_repo:
        passive = code_search.index_status(paths, project_id, repo=selected_git_repo)
        freshness = passive.get("freshness") or {}
        lexical_checks = freshness.get("lexical_checks") or {}
        semantic_snapshot_stale = passive.get("status") not in {"not_indexed", "not_found"} and (
            lexical_checks.get("engine") is False
            or lexical_checks.get("parser_profile") is False
            or lexical_checks.get("schema") is False
        )
        if semantic_snapshot_stale:
            refresh = code_index_jobs.start(paths.root, project_id, repo=selected_git_repo, force=False)
            return {
                "status": "refresh_started" if refresh.get("status") == "started" else refresh.get("status"),
                "project_id": project_id,
                "query": query,
                "reason": "the existing local structural/FTS snapshot uses stale engine/parser/schema semantics and is being refreshed in the detached index worker before search; rerun the search after completion",
                "index_status": passive,
                "refresh": refresh,
                "recommended_action": {
                    "tool": "code_index_refresh_status",
                    "arguments": {"name": project_id, "job_id": str((refresh.get("job") or {}).get("job_id") or "")},
                } if (refresh.get("job") or {}).get("job_id") else None,
            }
    if repo or source_id or len(sources) <= 1:
        result = code_search.search_project_code(
            paths, project_id, query, mode=mode, view=view, limit=limit,
            refresh_index=refresh_index, include_qdrant=True,
            use_fts=use_fts, use_qdrant=use_qdrant, use_reranker=use_reranker,
            result_focus=result_focus, structural_promotion=structural_promotion,
            strict_backends=strict_backends, max_chars=max_chars, repo=repo, source=source_id,
            diagnostic_targets=diagnostic_targets,
        )
        result = _finalize_code_diagnostics(paths, project_id, query, result)
        if capture_evidence:
            result = _capture_code_search_evidence(
                paths, project_id, result, repo=repo or selected_git_repo, source_id=source_id,
                acceptance_run_id=acceptance_run_id, session_id=session_id,
            )
        return result
    git_only = all(str(row.get("source_type") or "git") == "git" for row in sources)
    if git_only:
        per_repo: list[dict[str, Any]] = []
        hits: list[dict[str, Any]] = []
        each_limit = max(5, min(max(1, limit), 30))
        for row in sources:
            rid = str(row.get("repo_id") or row.get("source_id") or "")
            result = code_search.search_project_code(
                paths, project_id, query, mode=mode, view=view, limit=each_limit,
                refresh_index=refresh_index, include_qdrant=True,
                use_fts=use_fts, use_qdrant=use_qdrant, use_reranker=use_reranker,
                result_focus=result_focus, structural_promotion=structural_promotion,
                strict_backends=strict_backends, max_chars=max_chars, repo=rid,
                diagnostic_targets=diagnostic_targets,
            )
            result = _finalize_code_diagnostics(paths, project_id, query, result)
            per_repo.append({
                "repo_id": rid,
                "status": result.get("status"),
                "branch": result.get("scope") or result.get("branch"),
                "routing": result.get("routing"),
                "retrieval": (result.get("details") or {}).get("retrieval"),
                "diagnostic_trace": (result.get("details") or {}).get("candidate_trace"),
                "diagnostic_targets": (result.get("details") or {}).get("diagnostic_targets"),
            })
            for hit in result.get("hits") or []:
                item = dict(hit)
                item.setdefault("repo_id", rid)
                hits.append(item)
        hits.sort(key=lambda row: (-float(row.get("score") or 0.0), str(row.get("repo_id") or ""), str(row.get("path") or ""), int(row.get("start_line") or 0)))
        bad = [row for row in per_repo if row.get("status") not in {"ok", "current", "indexed"}]
        return {
            "status": "partial" if bad else "ok", "project_id": project_id, "query": query,
            "scope": {"repositories": [row.get("repo_id") for row in sources], "multi_repo": True},
            "hits": hits[:max(1, min(limit, 100))], "repositories": per_repo,
            "rules": ["Broad project code search spans all enabled registered repositories.", "Every hit retains repository identity; exact operations require repo= when ambiguous."],
        }
    per_source: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    each_limit = max(5, min(max(1, limit), 30))
    for row in sources:
        sid = str(row.get("source_id") or "")
        result = code_search.search_project_code(
            paths, project_id, query, mode=mode, view=view, limit=each_limit,
            refresh_index=refresh_index, include_qdrant=True,
            use_fts=use_fts, use_qdrant=use_qdrant, use_reranker=use_reranker,
            result_focus=result_focus, structural_promotion=structural_promotion,
            strict_backends=strict_backends, max_chars=max_chars, source=sid,
            diagnostic_targets=diagnostic_targets,
        )
        result = _finalize_code_diagnostics(paths, project_id, query, result)
        per_source.append({
            "source_id": sid,
            "source_type": row.get("source_type"),
            "repo_id": row.get("repo_id"),
            "status": result.get("status"),
            "revision": result.get("scope") or result.get("branch"),
            "routing": result.get("routing"),
            "retrieval": (result.get("details") or {}).get("retrieval"),
            "diagnostic_trace": (result.get("details") or {}).get("candidate_trace"),
            "diagnostic_targets": (result.get("details") or {}).get("diagnostic_targets"),
        })
        for hit in result.get("hits") or []:
            item = dict(hit)
            item.setdefault("source_id", sid)
            hits.append(item)
    hits.sort(key=lambda row: (-float(row.get("score") or 0.0), str(row.get("source_id") or row.get("repo_id") or ""), str(row.get("path") or ""), int(row.get("start_line") or 0)))
    bad = [row for row in per_source if row.get("status") not in {"ok", "current", "indexed"}]
    return {
        "status": "partial" if bad else "ok", "project_id": project_id, "query": query,
        "scope": {"sources": [row.get("source_id") for row in sources], "multi_source": True},
        "hits": hits[:max(1, min(limit, 100))], "sources": per_source,
        "rules": ["Broad project code search spans all enabled registered evidence sources.", "Every hit retains source/revision identity; exact operations require source_id= (or repo= for Git) when ambiguous."],
    }


def code_index_status(
    name: str = "",
    *,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    sources = project_workspace.project_sources(paths.root, project_id)
    if repo or source_id or len(sources) <= 1:
        return code_search.index_status(paths, project_id, repo=repo, source=source_id)
    if all(str(row.get("source_type") or "git") == "git" for row in sources):
        results = [code_search.index_status(paths, project_id, repo=str(row.get("repo_id") or row.get("source_id") or "")) for row in sources]
        return {"status": "ok", "project_id": project_id, "repositories": results, "multi_repo": True}
    results = [code_search.index_status(paths, project_id, source=str(row.get("source_id") or "")) for row in sources]
    return {"status": "ok", "project_id": project_id, "sources": results, "multi_source": True}



def code_index_verify(
    name: str = "",
    *,
    include_qdrant: bool = True,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Explicitly deep-verify source freshness and optional code-Qdrant reachability."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    sources = project_workspace.project_sources(paths.root, project_id)
    if repo or source_id or len(sources) <= 1:
        return code_search.index_status(
            paths, project_id, deep_verify=True, verify_qdrant=include_qdrant, repo=repo, source=source_id
        )
    if all(str(row.get("source_type") or "git") == "git" for row in sources):
        results = [
            code_search.index_status(
                paths, project_id, deep_verify=True, verify_qdrant=include_qdrant, repo=str(row.get("repo_id") or row.get("source_id") or "")
            )
            for row in sources
        ]
        bad = [row for row in results if row.get("status") not in {"ok", "current", "indexed"}]
        return {"status": "partial" if bad else "ok", "project_id": project_id, "repositories": results, "multi_repo": True}
    results = [
        code_search.index_status(
            paths, project_id, deep_verify=True, verify_qdrant=include_qdrant, source=str(row.get("source_id") or "")
        )
        for row in sources
    ]
    bad = [row for row in results if row.get("status") not in {"ok", "current", "indexed"}]
    return {"status": "partial" if bad else "ok", "project_id": project_id, "sources": results, "multi_source": True}


def code_definition(
    symbol: str,
    *,
    name: str = "",
    view: str = "context",
    limit: int = 10,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.definition_lookup(
        paths, project_id, symbol, view=view, limit=limit, refresh_index=refresh_index, repo=repo, source=source_id
    )


def code_callers(
    symbol: str,
    *,
    name: str = "",
    view: str = "context",
    limit: int = 20,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.callers_lookup(
        paths, project_id, symbol, view=view, limit=limit, refresh_index=refresh_index, repo=repo, source=source_id
    )


def code_callees(
    symbol: str,
    *,
    name: str = "",
    view: str = "context",
    limit: int = 20,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.callees_lookup(
        paths, project_id, symbol, view=view, limit=limit, refresh_index=refresh_index, repo=repo, source=source_id
    )


def code_path(
    source: str,
    target: str,
    *,
    name: str = "",
    view: str = "context",
    limit: int = 30,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.path_lookup(
        paths, project_id, source, target, view=view, limit=limit,
        refresh_index=refresh_index, repo=repo, source_id=source_id,
    )


def code_flow_graph(
    symbol: str,
    *,
    name: str = "",
    max_depth: int = 5,
    max_nodes: int = 120,
    max_edges: int = 400,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Build a bounded relevant structural graph rooted at one exact symbol."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.flow_graph_lookup(
        paths,
        project_id,
        symbol,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        refresh_index=refresh_index, repo=repo, source=source_id,
    )


def code_source_window(
    path: str,
    *,
    name: str = "",
    start_line: int = 1,
    end_line: int = 0,
    max_chars: int = 20000,
    max_line_chars: int = 4096,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Read a bounded hash-checked source range from the active indexed repository."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.source_window(
        paths,
        project_id,
        path,
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
        max_line_chars=max_line_chars,
        refresh_index=refresh_index, repo=repo, source=source_id,
    )


def code_evidence_verify(
    evidence_id: str,
    *,
    name: str = "",
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Verify that a code_source_window evidence id still names the same source snapshot."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return code_search.verify_evidence(paths, project_id, evidence_id, repo=repo, source=source_id)


def code_semantics_check(
    language: str,
    operation: str,
    inputs: dict[str, Any] | None = None,
    *,
    name: str = "",
    repo: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Run a fixed allow-listed Go semantics probe without executing repository code.

    Supported operations are advertised by the MCP schema/tool description and
    rejected explicitly when unknown.
    """
    paths = paths or HarnessPaths.from_env()
    selected = str(language or "").strip().lower()
    if selected not in {"go", "golang"}:
        return {
            "status": "rejected",
            "language": selected,
            "reason": "only Go allow-listed semantics probes are currently implemented",
            "supported_languages": ["go"],
        }
    result = code_search.check_go_semantics(operation, inputs or {})
    project_id = (
        project_workspace.clean_project_id(name)
        if name
        else attached_project_id(paths, session_id=session_id or None)
    )
    if project_id and project_workspace_path(paths, project_id=project_id, session_id=session_id or None) is not None:
        resolved = project_workspace.resolve_project_repository(paths.root, project_id, repo, require_unique=True)
        if resolved.get("status") == "ok":
            project_meta = code_search.read_project_go_metadata(Path(resolved["root"]))
            result = code_search.attach_project_toolchain_context(result, project_meta)
            result["repo_id"] = resolved.get("repo_id")
        else:
            result["project_toolchain"] = {"alignment": "unknown", "reason": resolved.get("reason", "repository selection required")}
        result["project_id"] = project_id
    safe_result, _ = safety.redact_analysis_nested(result)
    return safe_result


def code_exact_search(
    patterns: list[str],
    *,
    name: str = "",
    repo: str = "",
    mode: str = "matches",
    paths_filter: list[str] | None = None,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    ignore_case: bool = False,
    fixed_strings: bool = False,
    hidden: bool = False,
    include_ignored: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    offset: int = 0,
    limit: int = 200,
    timeout_seconds: float = 20.0,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Run structured repository-scoped ripgrep without shell command construction.

    This is the high-control exact-search companion to OpenCode Grep: multiple
    expressions, file/count modes, context, globs, hidden/ignored policy, and
    bounded pagination are explicit typed arguments.  It is not semantic
    retrieval and it never accepts arbitrary ripgrep CLI fragments.
    """
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    resolved = project_workspace.resolve_project_repository(paths.root, project_id, repo, require_unique=True)
    if resolved.get("status") != "ok":
        return {k: v for k, v in resolved.items() if k != "root"}
    repo_root = Path(resolved["root"])
    state = project_workspace.repository_root_status(repo_root)
    if state.get("invalid_repo_root"):
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_id": resolved.get("repo_id"),
            "reason": "configured repository root is not the exact Git worktree root",
            "repository": state,
        }
    result = code_search.exact_search(
        repo_root,
        patterns=patterns,
        mode=mode,
        paths=paths_filter or [],
        include_globs=include_globs or [],
        exclude_globs=exclude_globs or [],
        ignore_case=ignore_case,
        fixed_strings=fixed_strings,
        hidden=hidden,
        include_ignored=include_ignored,
        context_before=context_before,
        context_after=context_after,
        offset=offset,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )
    result["project_id"] = project_id
    result["repo_id"] = resolved.get("repo_id")
    result["repository_assurance"] = state
    return result


def code_text_search(
    pattern: str,
    *,
    name: str = "",
    paths_filter: list[str] | None = None,
    page_size: int = 1000,
    cursor: str = "",
    preview_chars: int = 320,
    ignore_case: bool = False,
    fixed_string: bool = False,
    include_ignored: bool = False,
    shard_timeout_seconds: float = 15.0,
    operation_timeout_seconds: float = 20.0,
    repo: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Exhaustively search permitted repository source with source-aware secret redaction and bounded paginated transport."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    return code_search.search_project_text(
        paths,
        project_id,
        pattern,
        search_paths=paths_filter or [],
        page_size=page_size,
        cursor=cursor,
        preview_chars=preview_chars,
        ignore_case=ignore_case,
        fixed_string=fixed_string,
        include_ignored=include_ignored,
        shard_timeout_seconds=shard_timeout_seconds,
        operation_timeout_seconds=operation_timeout_seconds, repo=repo,
    )


def cross_project_code_search(
    query: str,
    *,
    projects: list[str] | None = None,
    all_indexed: bool = False,
    mode: str = "auto",
    view: str = "context",
    limit: int = 20,
    refresh_stale: bool = False,
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return code_search.cross_project_search(
        paths,
        query,
        projects=projects or [],
        all_indexed=all_indexed,
        mode=mode,
        view=view,
        limit=limit,
        refresh_stale=refresh_stale,
    )


def code_validate_claim(
    claim: str,
    *,
    name: str = "",
    refresh_index: bool = False,
    repo: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Validate one atomic code claim without embeddings or reranking; broad verification requests are decomposed by the client."""
    paths = paths or HarnessPaths.from_env()
    project_id, error = _resolve_code_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    project_workspace.enable_code_index(paths.root, project_id)
    result = code_search.validate_claim(
        paths, project_id, claim, refresh_index=refresh_index, repo=repo
    )
    result.setdefault("project_id", project_id)
    safe_result, _ = safety.redact_source_nested(result)
    return safe_result

def code_evaluate(
    suite: str = "smoke",
    *,
    report_name: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Run a versioned structural code-search golden suite."""
    paths = paths or HarnessPaths.from_env()
    suites_root = (paths.root / ".harness" / "evaluation" / "code_search" / "suites").resolve()
    candidate = Path(suite)
    if candidate.suffix != ".jsonl":
        candidate = candidate.with_suffix(".jsonl")
    if not candidate.is_absolute():
        candidate = suites_root / candidate.name
    candidate = candidate.resolve()
    try:
        candidate.relative_to(suites_root)
    except ValueError:
        return {"status": "rejected", "reason": "suite must be under the Awoki code-search suites directory"}
    if not candidate.exists():
        return {"status": "not_found", "suite": str(candidate)}
    safe_report = re.sub(r"[^A-Za-z0-9_.-]+", "-", report_name or candidate.stem).strip(".-") or "code-search"
    report = paths.root / ".harness" / "evaluation" / "code_search" / "reports" / f"{safe_report}.json"
    return code_search.run_suite(paths, candidate, report_path=report)



def project_continuation_schedule(
    workflow: str,
    phase: str,
    wait_tool: str,
    wait_job_id: str,
    wait_seconds: int,
    *,
    name: str = "",
    repo: str = "",
    source_id: str = "",
    next_action: str = "",
    resume_goal: str = "",
    auto_resume: bool = True,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Schedule durable auto-continuation for one detached Awoki job.

    An explicit managed project may be supplied even when the session is not
    attached to it. Current detached structural/vector wait tools are project-scoped,
    so true ad-hoc/session-only work is rejected rather than silently creating or
    pretending to own a managed index/vector scope.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return continuations.schedule(
        paths.root, session_id, workflow=workflow, phase=phase, wait_tool=wait_tool,
        wait_job_id=wait_job_id, wait_seconds=wait_seconds, project_id=name, repo=repo,
        source_id=source_id, next_action=next_action, resume_goal=resume_goal,
        auto_resume=auto_resume,
    )


def project_continuation_status(
    *, session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Return the current session's durable detached-job continuation state."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return continuations.status(paths.root, session_id)


def project_continuation_cancel(
    reason: str = "cancelled", *, session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Cancel auto-continuation without cancelling the detached worker itself."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return continuations.cancel(paths.root, session_id, reason=reason)


def project_continuation_finalize(
    reason: str = "completed", *, session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Mark the current session continuation complete after the workflow advances."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return continuations.finalize(paths.root, session_id, reason=reason)

def session_work_status(
    *, session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Return durable OpenCode TODO/work state for this session, including unattached/ad-hoc sessions."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    return work_ledger.status(paths.root, session_id)


def _reference_project_id(
    paths: HarnessPaths, *, name: str = "", session_id: str = ""
) -> tuple[str, dict[str, Any] | None]:
    project_id = project_workspace.clean_project_id(name) if name else (attached_project_id(paths, session_id=session_id or None) or "")
    if not project_id:
        return "", {"status": "rejected", "reason": "Reference project scope is unavailable; supply name= or attach a managed project."}
    return project_id, None


def reference_describe(
    reference_id: str, *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Describe one durable Awoki reference without dumping its rich payload."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _reference_project_id(paths, name=name, session_id=session_id)
    if error:
        return error
    result = reference_catalog.describe(paths.root, project_id, reference_id, session_id=session_id)
    if result.get("status") == "ok" and session_id:
        work_ledger.touch_reference(
            paths.root, session_id, project_id=project_id,
            reference_id=str(result.get("reference_id") or reference_id),
            label=str(result.get("label") or ""), why_saved=str(result.get("why_saved") or ""),
        )
    return result


def reference_annotate(
    reference_id: str,
    *,
    label: str = "",
    why_saved: str = "",
    aliases: list[str] | None = None,
    linked_refs: list[str] | None = None,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Attach human navigation metadata to an existing stable Awoki ID.

    Labels/aliases never replace the underlying stable identity or provenance.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _reference_project_id(paths, name=name, session_id=session_id)
    if error:
        return error
    result = reference_catalog.annotate(
        paths.root, project_id, reference_id, label=label, why_saved=why_saved,
        aliases=aliases, linked_refs=linked_refs, session_id=session_id,
    )
    if result.get("status") == "ok" and session_id:
        work_ledger.touch_reference(
            paths.root, session_id, project_id=project_id,
            reference_id=str(result.get("reference_id") or reference_id),
            label=str(result.get("label") or ""), why_saved=str(result.get("why_saved") or ""),
        )
    return result


def reference_resolve(
    query: str, *, name: str = "", limit: int = 8, session_id: str = "", paths: HarnessPaths | None = None
) -> dict[str, Any]:
    """Resolve a human phrase to bounded candidate stable Awoki IDs.

    Resolution is navigation only; callers use the returned stable reference_id for
    authoritative evidence/state retrieval.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _reference_project_id(paths, name=name, session_id=session_id)
    if error:
        return error
    result = reference_catalog.resolve(paths.root, project_id, query, limit=limit, session_id=session_id)
    resolved = str(result.get("resolved_reference_id") or "")
    if resolved and session_id:
        match = next(
            (row for row in (result.get("matches") or []) if isinstance(row, dict) and str(row.get("reference_id") or "") == resolved),
            {},
        )
        work_ledger.touch_reference(
            paths.root, session_id, project_id=project_id, reference_id=resolved,
            label=str(match.get("label") or ""), why_saved=str(match.get("why_saved") or ""),
        )
    return result


def acceptance_run_start(
    suite: str,
    *,
    title: str = "",
    expected_tests: list[str] | None = None,
    expected_invariants: list[str] | None = None,
    test_plan: list[dict[str, Any]] | None = None,
    name: str = "",
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Start a project-scoped structured acceptance ledger that survives compaction."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _resolve_managed_project(paths, name=name, session_id=session_id)
    if error:
        return error
    assert project_id is not None
    return acceptance_runs.start(
        paths.root, project_id, suite=suite, title=title, repo=repo, source_id=source_id,
        expected_tests=expected_tests, expected_invariants=expected_invariants, test_plan=test_plan, session_id=session_id,
    )


def acceptance_run_next(
    run_id: str = "", *, name: str = "", session_id: str = "", paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Return the next unfinished acceptance step and its bounded orchestration contract."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else (attached_project_id(paths, session_id=session_id or None) or "")
    return acceptance_runs.next_step(paths.root, run_id=run_id, project_id=project_id, session_id=session_id)


def session_runtime_status(session_id: str = "", *, paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Return structural agent-turn anomaly/recovery metadata; private reasoning is never stored."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    result = agent_runtime.status(paths.root, session_id)
    work_state = work_ledger.status(paths.root, session_id) if session_id else {}
    if isinstance(work_state, dict):
        result["compaction"] = {
            "generation": int(work_state.get("compaction_generation") or 0),
            "last_compacted_at": str(work_state.get("last_compacted_at") or ""),
            "last_trigger": str(work_state.get("last_compaction_trigger") or "unknown"),
            "pending_trigger": str(work_state.get("pending_compaction_trigger") or ""),
        }
    active_acceptance = acceptance_runs.status(paths.root, session_id=session_id) if session_id else {}
    if isinstance(active_acceptance, dict) and active_acceptance.get("status") == "ok":
        result["acceptance_compaction"] = {
            "run_id": active_acceptance.get("run_id"),
            "generation_at_start": int(active_acceptance.get("compaction_generation_at_start") or 0),
            "generation": int(active_acceptance.get("compaction_generation") or 0),
            "count_since_run_start": int(active_acceptance.get("compaction_count") or 0),
            "last_compacted_at": str(active_acceptance.get("last_compacted_at") or ""),
            "events": [dict(row) for row in (active_acceptance.get("compaction_events") or []) if isinstance(row, dict)][-16:],
        }
    manifest_path = Path(os.getenv("AWOKI_OPENCODE_RUNTIME_MANIFEST", "/usr/local/share/awoki/opencode-runtime.json"))
    try:
        runtime = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(runtime, dict):
            result["opencode_runtime"] = {
                key: runtime.get(key)
                for key in ("install_mode", "channel_state", "requested_safe_version", "resolved_cli", "resolved_plugin", "resolved_sdk")
                if key in runtime
            }
    except (OSError, json.JSONDecodeError):
        result["opencode_runtime"] = {"status": "unavailable"}
    return result


def acceptance_run_status(
    run_id: str = "",
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Return the active or explicitly named acceptance ledger without relying on chat memory."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else (attached_project_id(paths, session_id=session_id or None) or "")
    return acceptance_runs.status(paths.root, run_id=run_id, project_id=project_id, session_id=session_id)


def acceptance_evidence_get(
    evidence_ref: str,
    *,
    run_id: str = "",
    name: str = "",
    selector: str = "payload",
    offset: int = 0,
    limit: int = 20,
    max_chars: int = 20_000,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Retrieve a bounded slice of an exact raw Awoki evidence artifact by stable ref.

    Evidence artifacts are project-local, content-addressed, stored below an
    artifacts/.../raw path that is never registered for RAG, and are intentionally
    retrieved only on demand so compaction/context stays small.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name) if name else ""
    if not project_id and run_id:
        current = acceptance_runs.status(paths.root, run_id=run_id, session_id=session_id)
        if current.get("status") == "ok":
            project_id = str(current.get("project_id") or "")
    if not project_id:
        current = acceptance_runs.status(paths.root, session_id=session_id)
        if current.get("status") == "ok":
            project_id = str(current.get("project_id") or "")
    if not project_id:
        project_id = attached_project_id(paths, session_id=session_id or None) or ""
    if not project_id:
        return {"status": "rejected", "reason": "Evidence project scope is unavailable; supply name= or run_id=."}
    return evidence_store.get(
        paths.root, project_id, evidence_ref, selector=selector, offset=offset, limit=limit, max_chars=max_chars
    )


def _acceptance_project_id(
    paths: HarnessPaths, *, run_id: str, name: str, session_id: str
) -> tuple[str, dict[str, Any] | None]:
    if name:
        return project_workspace.clean_project_id(name), None
    current = acceptance_runs.status(paths.root, run_id=run_id, session_id=session_id)
    project_id = str(current.get("project_id") or "") if current.get("status") == "ok" else ""
    if project_id:
        return project_id, None
    attached = attached_project_id(paths, session_id=session_id or None)
    if attached:
        return attached, None
    return "", {"status": "rejected", "reason": "Acceptance run scope is unavailable; supply name= or use the originating session."}


def acceptance_run_record(
    run_id: str,
    test_id: str,
    outcome: str,
    *,
    name: str = "",
    query: str = "",
    targets: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    candidate_ids: list[str] | None = None,
    primary_candidate_id: str = "",
    notes: str = "",
    violations: list[str] | None = None,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Persist one bounded structured test observation immediately after its tool evidence is observed."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _acceptance_project_id(paths, run_id=run_id, name=name, session_id=session_id)
    if error:
        return error
    return acceptance_runs.record(
        paths.root, run_id=run_id, project_id=project_id, test_id=test_id, outcome=outcome,
        query=query, targets=targets, evidence=evidence, evidence_refs=evidence_refs,
        candidate_ids=candidate_ids, primary_candidate_id=primary_candidate_id,
        notes=notes, violations=violations,
    )


def acceptance_run_record_invariant(
    run_id: str,
    invariant_id: str,
    outcome: str,
    *,
    name: str = "",
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Persist one structured cross-test invariant observation."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _acceptance_project_id(paths, run_id=run_id, name=name, session_id=session_id)
    if error:
        return error
    return acceptance_runs.record_invariant(
        paths.root, run_id=run_id, project_id=project_id, invariant_id=invariant_id, outcome=outcome,
        evidence=evidence, evidence_refs=evidence_refs,
    )


def acceptance_run_finalize(
    run_id: str,
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Finalize ledger completeness; this does not upgrade model-recorded observations into machine proof."""
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id, error = _acceptance_project_id(paths, run_id=run_id, name=name, session_id=session_id)
    if error:
        return error
    return acceptance_runs.finalize(paths.root, run_id=run_id, project_id=project_id)


def project_task_checkpoint(
    title: str,
    *,
    name: str = "",
    status: str = "running",
    current_step: str = "",
    completed_steps: list[str] | None = None,
    remaining_steps: list[str] | None = None,
    next_action: str = "",
    last_tool_output_summary: str = "",
    related_refs: list[str] | None = None,
    task_id: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    """Checkpoint generic long-running project work in canonical continuity.

    This is the neutral task primitive. Burp-specific task tools remain
    compatibility helpers for Burp workflows and must not be used for ordinary
    repository/document analysis.
    """
    paths = paths or HarnessPaths.from_env()
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return {"status": "rejected", "reason": "No project is attached."}
    clean_status = re.sub(r"[^a-z0-9_-]+", "-", str(status or "running").strip().lower()) or "running"
    if not task_id:
        task_id = "task_" + hashlib.sha256(f"{project_id}|{title}|{time.time_ns()}".encode()).hexdigest()[:16]
    task_id = re.sub(r"[^A-Za-z0-9._:-]+", "-", task_id).strip("-._:") or "task"
    details = "\n".join(filter(None, [
        f"Current step: {current_step}" if current_step else "",
        f"Last tool output: {last_tool_output_summary}" if last_tool_output_summary else "",
        "Completed: " + "; ".join(completed_steps or []) if completed_steps else "",
        "Remaining: " + "; ".join(remaining_steps or []) if remaining_steps else "",
    ]))
    saved = project_workspace.project_capture(
        paths.root, project_id,
        f"Task checkpoint: {title} ({clean_status}).",
        kind="continuity_reflection",
        details=details,
        sources=[],
        confidence="high",
        likely_continuation=next_action,
        tags=["task", "checkpoint"],
        state=clean_status,
        metadata={
            "adapter": "generic_task",
            "task_id": task_id,
            "title": title,
            "status": clean_status,
            "current_step": current_step,
            "completed_steps": completed_steps or [],
            "remaining_steps": remaining_steps or [],
            "last_tool_output_summary": last_tool_output_summary,
            "next_action": next_action,
            "related_refs": related_refs or [],
        },
    )
    return {
        "status": "checkpointed",
        "project_id": project_id,
        "task_id": task_id,
        "checkpoint_id": saved.get("id"),
        "task_status": clean_status,
        "next_action": next_action,
        "continue_command": f"project_task_status(task_id='{task_id}') then continue next_action",
    }


def project_task_status(
    task_id: str = "",
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    if not project_id:
        return {"status": "rejected", "reason": "No project is attached."}
    pp = project_workspace.paths_for(paths.root, project_id)
    rows = []
    for row in continuity.load_records(pp.memory_dir, project_id, include_legacy=False):
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if meta.get("adapter") != "generic_task":
            continue
        if task_id and str(meta.get("task_id") or "") != task_id:
            continue
        rows.append(row)
    if not rows:
        return {"status": "none", "project_id": project_id, "task_id": task_id}
    rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    row = rows[0]
    meta = dict(row.get("metadata") or {})
    return {
        "status": "ok",
        "project_id": project_id,
        "task_id": str(meta.get("task_id") or ""),
        "task_status": str(meta.get("status") or row.get("state") or ""),
        "title": str(meta.get("title") or row.get("summary") or ""),
        "current_step": str(meta.get("current_step") or ""),
        "completed_steps": list(meta.get("completed_steps") or []),
        "remaining_steps": list(meta.get("remaining_steps") or []),
        "last_tool_output_summary": str(meta.get("last_tool_output_summary") or ""),
        "next_action": str(meta.get("next_action") or row.get("likely_continuation") or ""),
        "related_refs": list(meta.get("related_refs") or []),
        "checkpoint_id": row.get("id"),
    }


def project_task_finalize(
    *,
    task_id: str = "",
    name: str = "",
    outcome: str = "",
    finding: str = "",
    next_action: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    current = project_task_status(task_id, name=name, session_id=session_id, paths=paths)
    if current.get("status") != "ok":
        return current
    project_id = str(current["project_id"])
    resolved_task_id = str(current["task_id"])
    title = str(current.get("title") or resolved_task_id)
    saved = project_workspace.project_capture(
        paths.root, project_id,
        finding or f"Task finalized: {title}.",
        kind="finding" if finding else "event",
        details=outcome,
        confidence="high" if finding else "medium",
        likely_continuation=next_action,
        tags=["task", "finalized"],
        state="done",
        metadata={
            "adapter": "generic_task",
            "task_id": resolved_task_id,
            "title": title,
            "status": "done",
            "outcome": outcome,
            "finding": finding,
            "next_action": next_action,
        },
    )
    return {
        "status": "finalized",
        "project_id": project_id,
        "task_id": resolved_task_id,
        "record_id": saved.get("id"),
        "outcome": outcome,
        "finding": finding,
        "next_action": next_action,
    }


def project_pause(
    name: str = "",
    *,
    summary: str = "",
    details: str = "",
    sources: list[Any] | None = None,
    uncertainty: list[str] | None = None,
    likely_continuation: str = "",
    confidence: str = "medium",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    safe_summary, summary_changed = redact_analysis_text(summary)
    safe_details, details_changed = redact_analysis_text(details)
    safe_sources, sources_changed = _redact_analysis_nested(sources or [])
    safe_uncertainty, uncertainty_changed = _redact_analysis_nested(uncertainty or [])
    safe_continuation, continuation_changed = redact_analysis_text(likely_continuation)
    if summary_changed or details_changed or sources_changed or uncertainty_changed or continuation_changed:
        confidence = "low" if confidence == "medium" else confidence
    return project_workspace.project_pause(
        paths.root,
        name,
        session_id=session_id or None,
        summary=safe_summary,
        details=safe_details,
        sources=safe_sources,
        uncertainty=safe_uncertainty,
        likely_continuation=safe_continuation,
        confidence=confidence,
    )


def project_index(name: str, include_artifacts: bool = True, include_code: bool = False, include_qdrant: bool = True, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    project_id = project_workspace.clean_project_id(name)
    if project_workspace_path(paths, project_id=project_id) is None:
        return {"status": "not_found", "project_id": project_id}
    return index_project(include_artifacts=include_artifacts, include_code=include_code, include_qdrant=include_qdrant, project_id=project_id, paths=paths)


def recall_context(query: str, include_global: bool = True, limit: int = 10, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    rag = search_rag(query=query, scope="project", include_global=include_global, limit=limit, session_id=session_id, paths=paths)
    skills = search_skills(query, scope="project_first", limit=5, paths=paths)
    return {
        "query": query,
        "attached_project": attached_project_id(paths, session_id=session_id or None),
        "project_first": True,
        "project_hits": rag["project_hits"],
        "global_hits": rag["global_hits"],
        "skill_candidates": skills,
        "retrieval": rag["retrieval"],
        "rules": [
            "Project knowledge overrides global knowledge.",
            "Use skills for procedures; use memory/RAG for facts.",
            "Sensitive values are stored only on explicit request and are excluded from automatic indexing and recall."
        ]
    }


def save_project_fact(text: str, evidence: str = "", tags: list[str] | None = None, confidence: str = "observed", sensitivity: str = "normal", allow_sensitive_plaintext: bool = False, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    pp = project_workspace_path(paths, session_id=session_id or None)
    if pp is None:
        return {
            "status": "rejected",
            "reason": "No project is attached. Project facts cannot silently fall back to legacy memory.",
        }
    classification = classify_memory_text("\n".join([text, evidence]), project_id=pp.project_id)
    if allow_sensitive_plaintext:
        safe_text, safe_evidence, changed = text, evidence, False
    else:
        safe_text, text_changed = redact_analysis_text(text)
        safe_evidence, evidence_changed = redact_analysis_text(evidence)
        changed = text_changed or evidence_changed
    captured = project_workspace.project_capture(
        paths.root,
        pp.project_id,
        safe_text,
        kind="fact",
        details=safe_evidence,
        confidence=confidence,
        sensitivity=sensitivity,
        index_policy="safe",
        tags=tags or [],
        sources=[],
        metadata={"classification": classification, "compatibility_tool": "save_project_fact"},
        refresh=False,
        allow_sensitive_plaintext=allow_sensitive_plaintext,
    )
    obj = {
        "scope": "project",
        "project_id": pp.project_id,
        "kind": "fact",
        "continuity_id": captured.get("id"),
        "text": safe_text,
        "evidence": safe_evidence,
        "tags": tags or [],
        "confidence": confidence,
        "sensitivity": "secret" if allow_sensitive_plaintext else sensitivity,
        "index_policy": "no_rag" if allow_sensitive_plaintext else "safe",
        "classification": classification,
    }
    saved = append_jsonl(pp.memory_dir / "facts.jsonl", obj)
    project_workspace.refresh_project_files(paths.root, pp.project_id)
    return {"status": "saved", **saved, "continuity": captured}


def save_global_fact(text: str, reason: str = "", tags: list[str] | None = None, reviewed: bool = False, allow_sensitive_plaintext: bool = False, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    classification = classify_memory_text(text)
    safe_text, text_redacted = (text, False) if allow_sensitive_plaintext else redact_analysis_text(text)
    safe_reason, reason_redacted = (reason, False) if allow_sensitive_plaintext else redact_analysis_text(reason)
    direct_allowed = os.environ.get("AWOKI_ALLOW_DIRECT_GLOBAL_SAVE") == "1" or os.environ.get("HARNESS_ALLOW_DIRECT_GLOBAL_SAVE") == "1"
    if not reviewed and not direct_allowed and not allow_sensitive_plaintext:
        candidate = propose_promotion(
            memory_text=safe_text, generalized_text=safe_text,
            reason=safe_reason or "direct global save requested; review required", paths=paths
        )
        return {"status":"needs_review", "reason":"Global writes require review by default. Candidate queued for approval.", "candidate":candidate}
    obj = {
        "scope":"global", "kind":"fact", "text":safe_text, "reason":safe_reason, "tags":tags or [],
        "confidence":"reviewed", "classification":classification,
        "sensitivity":"secret" if allow_sensitive_plaintext else "project",
        "index_policy":"no_rag" if allow_sensitive_plaintext else "safe",
        "explicit_sensitive_plaintext":bool(allow_sensitive_plaintext),
        "redaction_applied": bool(text_redacted or reason_redacted),
    }
    return append_jsonl(paths.global_memory_dir / "memories.jsonl", obj)


def save_finding(title: str, evidence: str, confidence: str = "hypothesis", tags: list[str] | None = None, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    pp = project_workspace_path(paths, session_id=session_id or None)
    if pp is None:
        return {"status": "rejected", "reason": "No project is attached. Findings require an explicit project."}
    safe_title, title_changed = redact_analysis_text(title)
    safe_evidence, evidence_changed = redact_analysis_text(evidence)
    changed = title_changed or evidence_changed
    captured = project_workspace.project_capture(
        paths.root,
        pp.project_id,
        safe_title,
        kind="finding",
        details=safe_evidence,
        confidence=confidence,
        sensitivity="project",
        index_policy="safe",
        tags=tags or [],
        metadata={"compatibility_tool": "save_finding"},
        refresh=False,
    )
    obj = {
        "scope": "project",
        "project_id": pp.project_id,
        "kind": "finding",
        "continuity_id": captured.get("id"),
        "title": safe_title,
        "text": safe_title,
        "evidence": safe_evidence,
        "confidence": confidence,
        "tags": tags or [],
        "sensitivity": "normal",
        "index_policy": "safe",
    }
    saved = append_jsonl(pp.memory_dir / "findings.jsonl", obj)
    project_workspace.refresh_project_files(paths.root, pp.project_id)
    return {"status": "saved", **saved, "continuity": captured}


def save_hypothesis(hypothesis: str, evidence: str = "", status: str = "open", tags: list[str] | None = None, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    pp = project_workspace_path(paths, session_id=session_id or None)
    if pp is None:
        return {"status": "rejected", "reason": "No project is attached. Hypotheses require an explicit project."}
    safe_hypothesis, hypothesis_changed = redact_analysis_text(hypothesis)
    safe_evidence, evidence_changed = redact_analysis_text(evidence)
    changed = hypothesis_changed or evidence_changed
    captured = project_workspace.project_capture(
        paths.root,
        pp.project_id,
        safe_hypothesis,
        kind="question",
        details=safe_evidence,
        confidence="low",
        sensitivity="project",
        index_policy="safe",
        tags=tags or [],
        uncertainty=[safe_hypothesis],
        state=status,
        metadata={"compatibility_tool": "save_hypothesis"},
        refresh=False,
    )
    obj = {
        "scope": "project",
        "project_id": pp.project_id,
        "kind": "hypothesis",
        "continuity_id": captured.get("id"),
        "hypothesis": safe_hypothesis,
        "text": safe_hypothesis,
        "evidence": safe_evidence,
        "status": status,
        "tags": tags or [],
        "sensitivity": "normal",
        "index_policy": "safe",
    }
    saved = append_jsonl(pp.memory_dir / "hypotheses.jsonl", obj)
    project_workspace.refresh_project_files(paths.root, pp.project_id)
    return {"status": "saved", **saved, "continuity": captured}


def propose_promotion(memory_text: str, generalized_text: str = "", reason: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    candidate_text = generalized_text or memory_text
    classification = classify_memory_text(candidate_text)
    safe_source, source_changed = redact_analysis_text(memory_text)
    safe_generalized, gen_changed = redact_analysis_text(generalized_text) if generalized_text else ("", False)
    safe_reason, reason_changed = redact_analysis_text(reason)
    obj = {
        "scope":"project",
        "project_id": active_project_id(paths),
        "kind":"promotion_candidate",
        "source_text": safe_source,
        "generalized_text": safe_generalized,
        "reason": safe_reason,
        "classification": classification,
        "status":"pending_review",
        "sensitivity": classification.get("sensitivity", "normal"),
        "redaction_applied": bool(source_changed or gen_changed or reason_changed),
    }
    return append_jsonl(paths.memory_dir / "promotion_candidates.jsonl", obj)


def _promotion_resolved_lines(paths: HarnessPaths) -> set[int]:
    resolved: set[int] = set()
    for rec in read_jsonl(paths.memory_dir / "promotion_candidates.jsonl"):
        if rec.get("kind") == "promotion_resolution" and rec.get("candidate_line") is not None:
            try:
                resolved.add(int(rec["candidate_line"]))
            except (TypeError, ValueError):
                continue
    return resolved


def list_promotion_candidates(paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    resolved = _promotion_resolved_lines(paths)
    return [
        r for r in read_jsonl(paths.memory_dir / "promotion_candidates.jsonl")
        if r.get("kind") == "promotion_candidate"
        and r.get("status") == "pending_review"
        and r.get("_line") not in resolved
    ]


def approve_promotion(candidate_line: int | None = None, generalized_text: str | None = None, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    candidates = list_promotion_candidates(paths)
    if not candidates:
        return {"status":"none", "reason":"No pending promotion candidates."}
    cand = None
    if candidate_line is not None:
        cand = next((c for c in candidates if c.get("_line") == candidate_line), None)
        if cand is None:
            return {"status":"not_found", "reason":f"No pending candidate at line {candidate_line}."}
    cand = cand or candidates[0]
    text = generalized_text or cand.get("generalized_text") or cand.get("source_text")
    result = save_global_fact(text=text, reason=cand.get("reason", "approved promotion"), tags=["promoted"], reviewed=True, paths=paths)
    if result.get("status") == "rejected":
        return {"status":"rejected", "reason":"Global save rejected by sensitivity guard.", "candidate":cand, "result":result}
    resolution = append_jsonl(paths.memory_dir / "promotion_candidates.jsonl", {
        "kind":"promotion_resolution",
        "status":"approved",
        "candidate_line": cand.get("_line"),
        "global_result": result,
    })
    append_jsonl(paths.global_memory_dir / "promotion_log.jsonl", {"kind":"promotion_approval","candidate":cand,"result":result,"resolution":resolution})
    return {"status":"approved", "candidate_line":cand.get("_line"), "promoted":result}


def reject_promotion(candidate_line: int | None = None, reason: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    candidates = list_promotion_candidates(paths)
    if not candidates:
        return {"status":"none", "reason":"No pending promotion candidates."}
    cand = None
    if candidate_line is not None:
        cand = next((c for c in candidates if c.get("_line") == candidate_line), None)
        if cand is None:
            return {"status":"not_found", "reason":f"No pending candidate at line {candidate_line}."}
    cand = cand or candidates[0]
    resolution = append_jsonl(paths.memory_dir / "promotion_candidates.jsonl", {
        "kind":"promotion_resolution",
        "status":"rejected",
        "candidate_line": cand.get("_line"),
        "reason": reason,
    })
    append_jsonl(paths.global_memory_dir / "promotion_log.jsonl", {"kind":"promotion_rejection","candidate":cand,"reason":reason,"resolution":resolution})
    return {"status":"rejected", "candidate_line":cand.get("_line"), "reason":reason}


def demote_global_memory(global_line: int, project_text: str = "", reason: str = "", tags: list[str] | None = None, session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Demote a global memory into project memory and hide it from future global recall.

    This is append-only: it does not delete global history. It writes a demotion marker
    to the global promotion log and a project-local fact preserving the scoped lesson.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    memory = next((r for r in read_jsonl(paths.global_memory_dir / "memories.jsonl") if r.get("_line") == global_line), None)
    if memory is None:
        return {"status":"not_found", "reason":f"No global memory found at line {global_line}."}
    text = project_text or memory.get("text", "")
    project_record = save_project_fact(
        text=text,
        evidence=f"Demoted from global memory line {global_line}. {reason}".strip(),
        tags=[*(tags or []), "demoted"],
        confidence="reviewed",
        sensitivity=memory.get("sensitivity", "normal"),
        session_id=session_id,
        paths=paths,
    )
    marker = append_jsonl(paths.global_memory_dir / "promotion_log.jsonl", {
        "kind":"global_memory_demotion",
        "global_line": global_line,
        "reason": reason,
        "original_memory": memory,
        "project_record": project_record,
    })
    return {"status":"demoted", "global_line":global_line, "project_record":project_record, "marker":marker}


def iter_skill_files(paths: HarnessPaths) -> Iterable[tuple[str, str, Path]]:
    for scope, base in [("project", paths.skills_dir), ("global", paths.global_skills_dir)]:
        if not base.exists():
            continue
        for skill in sorted(base.glob("*/SKILL.md")):
            yield scope, skill.parent.name, skill


def parse_skill(path: Path, scope: str, name: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    description = ""
    tags: list[str] = []
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1]
            for line in fm.splitlines():
                if line.strip().startswith("description:"):
                    description = line.split(":",1)[1].strip().strip('"')
                if line.strip().startswith("tags:"):
                    tags_line = line.split(":",1)[1].strip()
                    tags = [t.strip().strip("[] ,'") for t in tags_line.strip("[]").split(",") if t.strip()]
    if not description:
        for line in text.splitlines():
            if line.strip() and not line.startswith("#") and not line.startswith("---"):
                description = line.strip()[:240]
                break
    return {"scope":scope,"name":name,"path":str(path),"description":description,"tags":tags,"text":text}


def search_skills(query: str = "", scope: str = "project_first", limit: int = 10, paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    skills = [parse_skill(p, s, n) for s,n,p in iter_skill_files(paths)]
    if scope == "project":
        skills = [s for s in skills if s["scope"] == "project"]
    elif scope == "global":
        skills = [s for s in skills if s["scope"] == "global"]
    hits = []
    for s in skills:
        rec = {k:v for k,v in s.items() if k != "text"}
        rec["text"] = s["description"] + " " + " ".join(s.get("tags", [])) + " " + s["name"]
        score = score_record(query, rec)
        if query.strip() == "" or score > 0:
            out = {k:v for k,v in s.items() if k != "text"}
            out["score"] = score + (1.0 if s["scope"] == "project" and scope == "project_first" else 0.0)
            hits.append(out)
    hits.sort(key=lambda r: (r["score"], 1 if r["scope"] == "project" else 0), reverse=True)
    return hits[: max(1, min(limit, 50))]


def load_skill(name: str, scope: str | None = None, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    for s, n, p in iter_skill_files(paths):
        if n == name and (scope is None or s == scope):
            return parse_skill(p, s, n)
    return {"error": f"skill not found: {name}", "searched_scopes": ["project", "global"]}




def propose_skill_update(skill_name: str, proposed_change: str, reason: str = "", scope: str = "project", session_id: str = "", paths: HarnessPaths | None = None) -> dict[str, Any]:
    """Queue a reviewed skill update proposal without editing SKILL.md automatically.

    Skills are operational instructions, so Awoki treats changes to them more
    strictly than normal memory. This tool records a candidate for human review;
    it does not create, modify, or enable a skill by itself.
    """
    paths = paths or HarnessPaths.from_env()
    ensure_dirs(paths)
    if scope not in {"project", "global"}:
        return {"status": "rejected", "reason": "scope must be 'project' or 'global'"}
    safe_change, change_redacted = redact_analysis_text(proposed_change)
    safe_reason, reason_redacted = redact_analysis_text(reason)
    obj = {
        "kind": "skill_update_candidate",
        "scope": scope,
        "project_id": active_project_id(paths, session_id=session_id or None) if scope == "project" else None,
        "skill_name": skill_name,
        "proposed_change": safe_change,
        "reason": safe_reason,
        "status": "pending_review",
        "sensitivity": "normal",
        "redaction_applied": bool(change_redacted or reason_redacted),
        "note": "Review manually before editing SKILL.md. Awoki never auto-enables skill changes.",
    }
    return append_jsonl(paths.memory_dir / "skill_update_candidates.jsonl", obj)

def search_evidence(query: str, limit: int = 10, paths: HarnessPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or HarnessPaths.from_env()
    roots = [paths.artifacts_dir, paths.root / "corpora", paths.root / "docs"]
    records: list[dict[str, Any]] = []
    for base in roots:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            if p.suffix.lower() not in {".txt", ".md", ".json", ".jsonl", ".c", ".h", ".py", ".js", ".ts", ".java", ".go", ".rs", ".asm", ".s", ".log", ".yara", ".yar"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            safe_text, _ = redact_analysis_text(text[:20000])
            records.append({"scope":"project","kind":"artifact","path":str(p),"text":safe_text})
    hits = search_records(query, records, limit=limit)
    for h in hits:
        txt = h.get("text", "")
        h["preview"] = txt[:1200]
        h.pop("text", None)
    return hits



def search_code(
    query: str,
    path_glob: str = "",
    limit: int = 10,
    *,
    name: str = "",
    session_id: str = "",
    paths: HarnessPaths | None = None,
) -> list[dict[str, Any]]:
    """Legacy bounded lexical hint over the same coverage-first source policy.

    This compatibility tool is intentionally not the exhaustive contract; use
    code_text_search for complete counts/pagination. It must nevertheless never
    reintroduce generic secret-based source censorship.
    """
    paths = paths or HarnessPaths.from_env()
    project_id = project_workspace.clean_project_id(name) if name else attached_project_id(paths, session_id=session_id or None)
    attached = project_workspace_path(paths, project_id=project_id, session_id=session_id or None) if project_id else None
    if attached is not None:
        resolved = project_workspace.resolve_project_repository(paths.root, project_id, require_unique=False)
        if resolved.get("status") != "ok":
            return []
        root = Path(resolved["root"]).resolve()
    else:
        root = paths.root.resolve()
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if path_glob and not rel.match(path_glob):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        try:
            rel_root = p.relative_to(paths.root)
        except ValueError:
            rel_root = rel
        decision = indexing_policy.decide_file(
            p, rel=rel_root, category="code", redact=redact_text, max_bytes=2_000_000
        )
        if not decision.included:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:20000]
        except Exception:
            continue
        # Search locally over exact text so security-sensitive data files do not
        # become lexical blind spots. Sanitize only the returned representation.
        records.append({"scope":"project","kind":"code","path":str(rel),"text":text})
    hits = search_records(query, records, limit=limit)
    for h in hits:
        txt = str(h.get("text", ""))
        hit_path = root / str(h.get("path") or "")
        if indexing_policy.is_explicit_sensitive_path(hit_path):
            h["preview"] = "<REDACTED_SENSITIVE_FILE_CONTEXT>"
            h["redacted"] = True
        else:
            safe_txt, changed = safety.redact_source_text(txt)
            h["preview"] = safe_txt[:1200]
            h["redacted"] = changed
        h.pop("text", None)
    return hits




def open_artifact(path: str, max_bytes: int = 20000, redact_secrets: bool = True, paths: HarnessPaths | None = None) -> dict[str, Any]:
    paths = paths or HarnessPaths.from_env()
    root = paths.root.resolve()
    target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return {"error":"Refusing to open path outside harness root.", "path":str(target)}
    if not target.exists() or not target.is_file():
        return {"error":"File not found", "path":str(target)}
    data = target.read_bytes()[:max_bytes]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    if not redact_secrets and os.environ.get("AWOKI_ALLOW_RAW_ARTIFACT_OPEN") != "1":
        return {"error":"Raw artifact reads through MCP require AWOKI_ALLOW_RAW_ARTIFACT_OPEN=1. Use redact_secrets=true or inspect the file with an explicitly approved editor/read tool.", "path":str(target)}
    redacted = False
    if redact_secrets:
        target_posix = target.as_posix()
        if "/workspace/projects/" in target_posix and "/repo/" in target_posix:
            text, redacted = safety.redact_source_text(text)
        elif any(part in {"reports", "artifacts", "memory", "notes"} for part in target.parts):
            text, redacted = safety.redact_analysis_text(text)
        else:
            text, redacted = redact_text(text)
    return {"path":str(target),"bytes_returned":len(data),"truncated":target.stat().st_size>max_bytes,"redacted":redacted,"text":text}


# Keep exact project retrieval current after every canonical append. Semantic
# indexing remains explicit because embeddings may be expensive or unavailable.
_CAPTURE_SYNC_STATE = __import__("threading").local()


def _dynamic_exact_documents(
    paths: HarnessPaths,
    pp: project_workspace.ProjectPaths,
    *,
    include_views: bool,
) -> tuple[list[rag_backend.SearchDocument], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    docs: list[rag_backend.SearchDocument] = []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    try:
        meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    policy = meta.get("rag") if isinstance(meta.get("rag"), dict) else {}
    memory_allowed = bool(policy.get("index_memory", True))

    source_paths: set[str] = {
        str(pp.memory_dir / filename)
        for filename in ["continuity.jsonl", *continuity.LEGACY_MEMORY_FILES.keys()]
    }
    for record in project_workspace.continuity_records(pp):
        source = str(record.get("_source_file") or pp.continuity)
        source_paths.add(source)
        entry = {
            "path": source,
            "record_id": record.get("id"),
            "kind": record.get("kind"),
            "content_hash": record.get("fingerprint") or rag_backend.stable_doc_id(record_text(record)),
        }
        if not memory_allowed:
            entry["reason"] = "project_policy:index_memory=false"
            excluded.append(entry)
            continue
        doc = _record_to_doc(record, "project", pp.project_id)
        if doc is None:
            entry["reason"] = _record_index_reason(record)
            excluded.append(entry)
        else:
            entry["reason"] = "safe_record"
            included.append(entry)
            docs.append(doc)

    if include_views:
        files: list[Path] = [pp.project_dir / "README.md", pp.project_dir / "AGENTS.md"]
        if bool(policy.get("index_situation", True)):
            files.append(pp.situation)
        if bool(policy.get("index_handoff", True)):
            files.append(pp.handoff)
        if bool(policy.get("index_notes", True)):
            files.append(pp.notes_dir / "thoughts.md")
        decisions: list[dict[str, Any]] = []
        docs.extend(_text_file_documents(paths, files, "project", pp.project_id, "project_view", decisions=decisions))

        # Delete old rows for every dynamic view even when project policy has
        # just disabled it. Selected files are reinserted only when allowed.
        for file in [pp.project_dir / "README.md", pp.project_dir / "AGENTS.md", pp.situation, pp.handoff, pp.notes_dir / "thoughts.md"]:
            try:
                source_paths.add(str(file.relative_to(paths.root)))
            except ValueError:
                source_paths.add(str(file))
        for decision in decisions:
            (included if decision.get("included") else excluded).append(decision)
    return docs, included, excluded, source_paths


def _synchronize_exact_index_after_capture(root: Path, project_id: str, record: dict[str, Any] | Any) -> dict[str, Any]:
    active = getattr(_CAPTURE_SYNC_STATE, "active", set())
    key = (str(root.resolve()), project_id)
    if key in active:
        return {"status": "skipped", "reason": "reentrant_exact_index_sync"}
    active = set(active)
    active.add(key)
    _CAPTURE_SYNC_STATE.active = active
    try:
        paths = HarnessPaths(root=root.resolve(), global_root=expand(os.environ.get("AWOKI_GLOBAL_ROOT") or DEFAULT_GLOBAL_ROOT).resolve())
        pp = project_workspace.paths_for(paths.root, project_id)
        prior = indexing_policy.read_index_manifest(pp.index_manifest)
        include_artifacts = bool(prior.get("include_artifacts", True))
        include_code = bool(prior.get("include_code", False))
        try:
            current_meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_meta = {}
        current_policy_hash = project_workspace.rag_policy_hash(current_meta)
        if not prior:
            include_views = bool(record.get("_views_refreshed"))
            docs, _, _, dynamic_sources = _dynamic_exact_documents(paths, pp, include_views=include_views)
            fts = rag_backend.replace_fts_sources(
                project_fts_db(paths, project_id=project_id),
                docs,
                source_paths=dynamic_sources,
                scope="project",
            )
            return {
                "status": "incrementally_indexed",
                "project_id": project_id,
                "fts": fts,
                "manifest_fresh": False,
                "reason": "No full project index manifest exists yet; canonical continuity is exact-searchable and project_search/project_refresh will build the complete safe document set.",
            }

        general_include_code = bool(prior.get("general_include_code", False))
        source_probe = project_workspace.workspace_index_probe(
            pp,
            include_artifacts=include_artifacts,
            include_code=general_include_code,
            include_generated=False,
        )
        if (
            str(prior.get("source_probe_hash") or "") != source_probe["hash"]
            or str(prior.get("project_policy_hash") or "") != current_policy_hash
        ):
            return index_project(
                include_artifacts=include_artifacts,
                include_code=include_code,
                include_qdrant=False,
                project_id=project_id,
                paths=paths,
            )

        include_views = bool(record.get("_views_refreshed"))
        docs, dynamic_included, dynamic_excluded, dynamic_sources = _dynamic_exact_documents(paths, pp, include_views=include_views)
        fts = rag_backend.replace_fts_sources(
            project_fts_db(paths, project_id=project_id),
            docs,
            source_paths=dynamic_sources,
            scope="project",
        )
        if not include_views:
            return {"status": "record_indexed", "project_id": project_id, "fts": fts, "manifest_fresh": False}

        meta = current_meta
        workspace_generation = int((meta.get("continuity") or {}).get("workspace_generation") or 0)
        indexed_at = now_ts()
        index_generation = int(prior.get("index_generation") or 0) + 1
        policy_name = "fail_closed_allowlist"
        stamped_included = _stamp_index_entries(dynamic_included, project_id=project_id, indexed_at=indexed_at, index_generation=index_generation, policy=policy_name)
        stamped_excluded = _stamp_index_entries(dynamic_excluded, project_id=project_id, indexed_at=indexed_at, index_generation=index_generation, policy=policy_name)

        def keep_static(item: dict[str, Any]) -> bool:
            return not item.get("record_id") and str(item.get("path") or "") not in dynamic_sources

        included = [dict(item) for item in prior.get("included", []) if isinstance(item, dict) and keep_static(item)] + stamped_included
        excluded = [dict(item) for item in prior.get("excluded", []) if isinstance(item, dict) and keep_static(item)] + stamped_excluded
        probe = project_workspace.workspace_index_probe(
            pp, include_artifacts=include_artifacts, include_code=general_include_code
        )
        document_hash = rag_backend.fts_document_set_hash(project_fts_db(paths, project_id=project_id), scope="project")
        prior_qdrant = ((prior.get("backends") or {}).get("qdrant") or {}) if isinstance(prior, dict) else {}
        qdrant_state = dict(prior_qdrant)
        if qdrant_state.get("document_set_hash") != document_hash:
            qdrant_state.update({
                "status": "stale",
                "reason": "canonical continuity changed after the last semantic index build",
                "last_attempt_at": indexed_at,
            })
        manifest = {
            **prior,
            "schema_version": max(2, int(prior.get("schema_version") or 1)),
            "project_id": project_id,
            "index_generation": index_generation,
            "workspace_generation": workspace_generation,
            "indexed_at": indexed_at,
            "policy": policy_name,
            "project_policy_hash": current_policy_hash,
            "include_artifacts": include_artifacts,
            "include_code": include_code,
            "general_include_code": general_include_code,
            "workspace_probe_hash": probe["hash"],
            "workspace_probe_file_count": probe["file_count"],
            "source_probe_hash": source_probe["hash"],
            "source_probe_file_count": source_probe["file_count"],
            "included": included,
            "excluded": excluded,
            "document_count": rag_backend.fts_document_count(project_fts_db(paths, project_id=project_id), scope="project"),
            "document_set_hash": document_hash,
            "change_set": _index_change_set(prior, included),
            "backends": {
                "fts": {
                    "status": "indexed",
                    "document_set_hash": document_hash,
                    "indexed_at": indexed_at,
                    "index_generation": index_generation,
                    "workspace_generation": workspace_generation,
                },
                "qdrant": qdrant_state,
            },
        }
        indexing_policy.write_index_manifest(pp.index_manifest, manifest)
        return {
            "status": "incrementally_indexed",
            "project_id": project_id,
            "index_generation": index_generation,
            "workspace_generation": workspace_generation,
            "fts": fts,
        }
    finally:
        active = set(getattr(_CAPTURE_SYNC_STATE, "active", set()))
        active.discard(key)
        _CAPTURE_SYNC_STATE.active = active


project_workspace.register_capture_hook(_synchronize_exact_index_after_capture)
