from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

import continuity
import indexing_policy
import safety
import runtime_safety

SESSION_ID = os.environ.get("AWOKI_SESSION_ID") or f"awoki-session-{os.getpid()}-{uuid.uuid4().hex[:8]}"
_CAPTURE_HOOK: Callable[[Path, str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None

_GIT_REPOSITORY_ENV_OVERRIDES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


def _passive_git_env() -> dict[str, str]:
    env = runtime_safety.credential_free_environment()
    for key in _GIT_REPOSITORY_ENV_OVERRIDES:
        env.pop(key, None)
    transient = {
        "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
        "GIT_EXTERNAL_DIFF", "GIT_SHALLOW_FILE", "GIT_QUARANTINE_PATH", "GIT_GRAFT_FILE",
        "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
        "GIT_ATTR_NOSYSTEM", "GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS",
    }
    for key in list(env):
        if key in transient or key.startswith("GIT_TRACE") or key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = ""
    return env


def register_capture_hook(callback: Callable[[Path, str, Mapping[str, Any]], Mapping[str, Any] | None] | None) -> None:
    """Register the optional exact-index synchronization hook.

    The workspace layer remains usable by itself. The full harness registers a
    hook that keeps project FTS current after canonical captures.
    """
    global _CAPTURE_HOOK
    _CAPTURE_HOOK = callback


def now_ts() -> str:
    return continuity.now_ts()


def clean_project_id(name: str) -> str:
    value = (name or "").strip().replace(" ", "-")
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-._:")
    if not value:
        raise ValueError("project name cannot be empty")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"invalid project name: {name!r}")
    return value


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    project_id: str

    @property
    def project_dir(self) -> Path:
        return self.root / "workspace" / "projects" / self.project_id

    @property
    def memory_dir(self) -> Path:
        return self.project_dir / "memory"

    @property
    def continuity(self) -> Path:
        return self.memory_dir / "continuity.jsonl"

    @property
    def notes_dir(self) -> Path:
        return self.project_dir / "notes"

    @property
    def artifacts_dir(self) -> Path:
        return self.project_dir / "artifacts"

    @property
    def corpora_dir(self) -> Path:
        return self.project_dir / "corpora"

    @property
    def sources_dir(self) -> Path:
        return self.project_dir / "sources"

    @property
    def index_dir(self) -> Path:
        return self.project_dir / "index"

    @property
    def index_manifest(self) -> Path:
        return self.index_dir / "manifests" / "project-index.json"

    @property
    def situation(self) -> Path:
        return self.project_dir / "SITUATION.md"

    @property
    def handoff(self) -> Path:
        return self.project_dir / "HANDOFF.md"

    @property
    def project_json(self) -> Path:
        return self.project_dir / "project.json"


def projects_dir(root: Path) -> Path:
    return root / "workspace" / "projects"


def state_dir(root: Path) -> Path:
    return root / ".harness" / "state"


def sessions_dir(root: Path) -> Path:
    return state_dir(root) / "sessions"


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def session_state_path(root: Path, session_id: str | None = None) -> Path:
    return sessions_dir(root) / f"{_session_key(session_id or SESSION_ID)}.json"


def legacy_session_state_path(root: Path) -> Path:
    return state_dir(root) / "session_project.json"


def last_project_path(root: Path) -> Path:
    return state_dir(root) / "last_project.json"


def paths_for(root: Path, name: str) -> ProjectPaths:
    return ProjectPaths(root=root, project_id=clean_project_id(name))


def _write_text_if_missing(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    """Serialize updates to one state or metadata file across MCP processes."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return continuity.read_jsonl(path)


def append_jsonl(path: Path, obj: dict[str, Any]) -> dict[str, Any]:
    """Compatibility JSONL append used by Burp and older typed-memory callers."""
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


def _default_project_meta(project_id: str) -> dict[str, Any]:
    created = now_ts()
    return {
        "schema_version": 4,
        "project_id": project_id,
        "name": project_id,
        "status": "active",
        "created_at": created,
        "updated_at": created,
        "description": "",
        "default_scope": "project",
        "global_memory_allowed": True,
        "continuity": {
            "workspace_generation": 0,
            "snapshot_generation": 0,
            "last_record_id": "",
            "last_record_at": "",
            "last_handoff_record_id": "",
            "last_snapshot_change_ids": [],
        },
        "rag": {
            "policy": "fail_closed_allowlist",
            "index_situation": True,
            "index_handoff": True,
            "index_notes": True,
            "index_memory": True,
            "index_corpora": True,
            "index_reports": True,
            "index_registered_artifacts": True,
            "index_code": False,
            "index_skills": False,
            "exclude_raw": True,
        },
        "repositories": {"mode": "legacy", "default": "default", "items": {}},
        "sources": {"default": "", "items": {}},
        "tags": [],
    }


def _upgrade_meta(meta: dict[str, Any], project_id: str) -> dict[str, Any]:
    base = _default_project_meta(project_id)
    merged = {**base, **meta}
    merged["schema_version"] = max(4, int(meta.get("schema_version") or 1))
    merged["continuity"] = {**base["continuity"], **(meta.get("continuity") or {})}
    merged["rag"] = {**base["rag"], **(meta.get("rag") or {})}
    repositories = meta.get("repositories") if isinstance(meta.get("repositories"), Mapping) else {}
    items = repositories.get("items") if isinstance(repositories, Mapping) and isinstance(repositories.get("items"), Mapping) else {}
    mode = str(repositories.get("mode") or ("registered" if items else "legacy")) if isinstance(repositories, Mapping) else "legacy"
    default_repo = str(repositories.get("default") or (next(iter(items), "default"))) if isinstance(repositories, Mapping) else "default"
    merged["repositories"] = {
        "mode": mode if mode in {"legacy", "registered"} else "legacy",
        "default": default_repo,
        "items": {str(k): dict(v) for k, v in items.items() if isinstance(v, Mapping)},
    }
    sources = meta.get("sources") if isinstance(meta.get("sources"), Mapping) else {}
    source_items = sources.get("items") if isinstance(sources, Mapping) and isinstance(sources.get("items"), Mapping) else {}
    merged["sources"] = {
        "default": str(sources.get("default") or "") if isinstance(sources, Mapping) else "",
        "items": {str(k): dict(v) for k, v in source_items.items() if isinstance(v, Mapping)},
    }
    return merged


def ensure_project_layout(root: Path, name: str) -> ProjectPaths:
    pp = paths_for(root, name)
    dirs = [
        pp.project_dir,
        pp.notes_dir,
        pp.notes_dir / "sessions",
        pp.memory_dir,
        pp.project_dir / "repo",
        pp.sources_dir,
        pp.corpora_dir / "docs",
        pp.corpora_dir / "code",
        pp.corpora_dir / "reports",
        pp.artifacts_dir / "code",
        pp.artifacts_dir / "docs",
        pp.artifacts_dir / "evidence",
        pp.artifacts_dir / "reports",
        pp.artifacts_dir / "screenshots",
        pp.project_dir / "reports",
        pp.project_dir / "templates",
        pp.project_dir / "scratch",
        pp.index_dir / "sqlite",
        pp.index_dir / "manifests",
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
        _write_text_if_missing(directory / "README.md", f"# {pp.project_id} / {directory.relative_to(pp.project_dir)}\n\nAwoki project workspace directory.\n")
    for filename in ["continuity.jsonl", "facts.jsonl", "findings.jsonl", "hypotheses.jsonl", "decisions.jsonl", "events.jsonl", "pending.jsonl"]:
        _touch(pp.memory_dir / filename)
    _touch(pp.index_dir / "safe_artifacts.jsonl")
    _write_text_if_missing(pp.notes_dir / "thoughts.md", f"# Thoughts: {pp.project_id}\n\n")
    _write_text_if_missing(pp.project_dir / "README.md", f"# Project: {pp.project_id}\n\nAwoki continuity-first project workspace.\n")
    # Older Awoki releases created a project-local AGENTS.md containing only two
    # generic rules that are already enforced by the top-level agent contract.
    # OpenCode may re-inject project-scoped AGENTS.md on every source read, so
    # retaining that boilerplate creates repeated context cost without adding a
    # project-specific instruction. Remove only the exact Awoki-generated legacy
    # file; a user-authored/project-specific AGENTS.md is always preserved.
    project_agents = pp.project_dir / "AGENTS.md"
    legacy_project_agents = (
        f"# Project Rules: {pp.project_id}\n\n"
        "Project-local knowledge overrides global assumptions. "
        "User direction overrides suggested continuation.\n"
    )
    if project_agents.exists():
        try:
            if project_agents.read_text(encoding="utf-8") == legacy_project_agents:
                project_agents.unlink()
        except OSError:
            pass
    with _state_lock(pp.project_json):
        if not pp.project_json.exists():
            _write_json(pp.project_json, _default_project_meta(pp.project_id))
        else:
            current = _read_json(pp.project_json)
            upgraded = _upgrade_meta(current, pp.project_id)
            if upgraded != current:
                _write_json(pp.project_json, upgraded)
    return pp



_SOURCE_TYPES = {"directory", "corpus", "smali", "assembly", "pseudocode"}


def _clean_source_id(value: str) -> str:
    source_id = clean_project_id(value)
    if source_id in {"repo", "sources"}:
        raise ValueError(f"source id {source_id!r} is reserved")
    return source_id


def _source_path(root: Path, rel: Path) -> Path:
    absolute = (root / rel).resolve()
    absolute.relative_to(root.resolve())
    return absolute


def source_manifest_identity(source_root: Path) -> dict[str, Any]:
    """Hash one filesystem corpus deterministically without following symlinks.

    The identity is a canonical manifest of relative path, byte length, and
    SHA-256. It is intentionally independent of mtimes/inodes so copying the
    same corpus produces the same content identity.
    """
    original_root = source_root
    if not original_root.exists() or original_root.is_symlink():
        return {"status": "not_found", "content_identity": "", "file_count": 0, "manifest": []}
    source_root = original_root.resolve()
    candidates = [source_root] if source_root.is_file() else sorted(source_root.rglob("*"), key=lambda p: p.as_posix())
    manifest: list[dict[str, Any]] = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            rel = path.name if source_root.is_file() else path.relative_to(source_root).as_posix()
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
        except (OSError, ValueError):
            continue
        manifest.append({"path": rel, "size_bytes": size, "sha256": digest.hexdigest()})
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    identity = hashlib.sha256(canonical).hexdigest()
    return {
        "status": "ok",
        "content_identity": identity,
        "revision_key": f"sha256:{identity}",
        "file_count": len(manifest),
        "manifest": manifest,
    }


def project_source_registry(root: Path, name: str) -> dict[str, Any]:
    """Return generic evidence sources, deriving Git sources from repo registry."""
    pp = ensure_project_layout(root, name)
    meta = _meta(pp)
    configured = dict(meta.get("sources") or {})
    explicit = dict(configured.get("items") or {})
    items: dict[str, dict[str, Any]] = {}
    for row in project_repositories(root, name, enabled_only=False):
        rid = str(row.get("repo_id") or "")
        if not rid:
            continue
        items[rid] = {
            "path": str(row.get("path") or ""),
            "enabled": bool(row.get("enabled", True)),
            "source_type": "git",
            "repo_id": rid,
            "legacy": bool(row.get("legacy", False)),
            "derived_from_repository_registry": True,
        }
    for source_id, raw in explicit.items():
        if source_id in items:
            # Repository ids own their compatibility source id. Refuse silent
            # shadowing of a managed Git source by arbitrary corpus metadata.
            continue
        item = dict(raw or {})
        item["source_type"] = str(item.get("source_type") or "directory")
        items[str(source_id)] = item
    default = str(configured.get("default") or "")
    if not default:
        repo_default = str(project_repository_registry(root, name).get("default") or "")
        default = repo_default if repo_default in items else (next(iter(sorted(items)), ""))
    return {"default": default, "items": items}


def project_sources(root: Path, name: str, *, enabled_only: bool = True) -> list[dict[str, Any]]:
    pp = ensure_project_layout(root, name)
    registry = project_source_registry(root, name)
    rows: list[dict[str, Any]] = []
    for source_id, raw in sorted(dict(registry.get("items") or {}).items()):
        item = dict(raw or {})
        if enabled_only and not bool(item.get("enabled", True)):
            continue
        rel = Path(str(item.get("path") or ""))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            continue
        try:
            absolute = _source_path(pp.project_dir, rel)
        except ValueError:
            continue
        rows.append({
            "source_id": str(source_id),
            "source_type": str(item.get("source_type") or "directory"),
            "path": rel.as_posix(),
            "root": absolute,
            "enabled": bool(item.get("enabled", True)),
            "default": str(source_id) == str(registry.get("default") or ""),
            "repo_id": str(item.get("repo_id") or ""),
            "legacy": bool(item.get("legacy", False)),
            "derived_from_repository_registry": bool(item.get("derived_from_repository_registry", False)),
        })
    return rows


def resolve_project_source(
    root: Path,
    name: str,
    source_id: str = "",
    *,
    repo_id: str = "",
    require_unique: bool = True,
) -> dict[str, Any]:
    if repo_id:
        repo = resolve_project_repository(root, name, repo_id, require_unique=require_unique)
        if repo.get("status") != "ok":
            return repo
        return {
            **repo,
            "source_id": str(repo.get("repo_id") or ""),
            "source_type": "git",
            "repo_id": str(repo.get("repo_id") or ""),
            "derived_from_repository_registry": True,
        }
    registry = project_source_registry(root, name)
    rows = project_sources(root, name)
    requested = str(source_id or "").strip()
    if requested:
        requested = _clean_source_id(requested)
        matches = [row for row in rows if row["source_id"] == requested]
        if not matches:
            return {"status": "not_found", "project_id": clean_project_id(name), "source_id": requested, "reason": "source is not registered or enabled"}
        return {"status": "ok", "project_id": clean_project_id(name), **matches[0]}
    default_id = str(registry.get("default") or "")
    if default_id:
        defaults = [row for row in rows if row["source_id"] == default_id]
        if defaults and (not require_unique or len(rows) <= 1):
            return {"status": "ok", "project_id": clean_project_id(name), **defaults[0]}
    if len(rows) == 1:
        return {"status": "ok", "project_id": clean_project_id(name), **rows[0]}
    if not rows:
        return {"status": "not_found", "project_id": clean_project_id(name), "reason": "project has no enabled evidence sources"}
    if require_unique:
        if rows and all(str(row.get("source_type") or "git") == "git" for row in rows):
            # Preserve the established multi-Git failure contract for callers
            # that have not opted into the generic source selector yet.
            return resolve_project_repository(root, name, "", require_unique=True)
        return {
            "status": "ambiguous_source",
            "project_id": clean_project_id(name),
            "reason": "multiple evidence sources are registered; provide source_id= explicitly",
            "sources": [{k: v for k, v in row.items() if k != "root"} for row in rows],
        }
    if default_id:
        defaults = [row for row in rows if row["source_id"] == default_id]
        if defaults:
            return {"status": "ok", "project_id": clean_project_id(name), **defaults[0]}
    return {"status": "ok", "project_id": clean_project_id(name), **rows[0]}


def project_source_add(
    root: Path,
    name: str,
    source_id: str,
    path: str,
    *,
    source_type: str = "directory",
    default: bool = False,
) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    sid = _clean_source_id(source_id)
    if any(
        str(row.get("repo_id") or "") == sid
        for row in project_repositories(root, name, enabled_only=False)
    ):
        raise ValueError("source id conflicts with a registered Git repository id")
    kind = str(source_type or "directory").strip().lower()
    if kind not in _SOURCE_TYPES:
        raise ValueError(f"unsupported source type: {source_type!r}")
    rel = Path(str(path or "").strip().replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "sources":
        raise ValueError("non-Git source path must be project-relative under sources/")
    unresolved = pp.project_dir / rel
    if unresolved.is_symlink():
        raise ValueError("source path must not be a symlink")
    absolute = unresolved.resolve()
    try:
        absolute.relative_to(pp.sources_dir.resolve())
    except ValueError as exc:
        raise ValueError("non-Git source path must stay under project sources/") from exc
    if not absolute.exists() or not absolute.is_dir():
        raise ValueError("source path must exist and be a regular directory")
    identity = source_manifest_identity(absolute)
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        sources = dict(meta.get("sources") or {})
        items = dict(sources.get("items") or {})
        items[sid] = {"path": rel.as_posix(), "enabled": True, "source_type": kind}
        sources["items"] = items
        if default or not str(sources.get("default") or ""):
            sources["default"] = sid
        meta["sources"] = sources
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {
        "status": "registered",
        "project_id": pp.project_id,
        "source_id": sid,
        "source_type": kind,
        "path": rel.as_posix(),
        "content_identity": identity.get("content_identity", ""),
        "revision_key": identity.get("revision_key", ""),
        "file_count": identity.get("file_count", 0),
        "default": str(project_source_registry(root, name).get("default") or "") == sid,
    }


def project_source_remove(root: Path, name: str, source_id: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    sid = _clean_source_id(source_id)
    if any(str(row.get("repo_id") or "") == sid for row in project_repositories(root, name, enabled_only=False)):
        raise ValueError("Git-backed sources are managed through project_repo_remove")
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        sources = dict(meta.get("sources") or {})
        items = dict(sources.get("items") or {})
        if sid not in items:
            return {"status": "not_found", "project_id": pp.project_id, "source_id": sid}
        items.pop(sid, None)
        sources["items"] = items
        if str(sources.get("default") or "") == sid:
            sources["default"] = next(iter(sorted(items)), "")
        meta["sources"] = sources
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {"status": "removed", "project_id": pp.project_id, "source_id": sid}


def project_source_default(root: Path, name: str, source_id: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    sid = _clean_source_id(source_id)
    if not any(row["source_id"] == sid for row in project_sources(root, name, enabled_only=False)):
        return {"status": "not_found", "project_id": pp.project_id, "source_id": sid}
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        sources = dict(meta.get("sources") or {})
        sources["default"] = sid
        meta["sources"] = sources
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {"status": "updated", "project_id": pp.project_id, "source_id": sid, "default": True}


def _clean_repo_id(value: str) -> str:
    repo_id = clean_project_id(value)
    if repo_id == "repo":
        raise ValueError("repository id 'repo' is reserved")
    return repo_id


def project_repository_registry(root: Path, name: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    meta = _meta(pp)
    repositories = dict(meta.get("repositories") or {})
    mode = str(repositories.get("mode") or "legacy")
    items = dict(repositories.get("items") or {})
    if mode == "legacy":
        return {
            "mode": "legacy",
            "default": "default",
            "items": {
                "default": {
                    "path": "repo",
                    "enabled": True,
                    "legacy": True,
                }
            },
        }
    return {
        "mode": "registered",
        "default": str(repositories.get("default") or ""),
        "items": items,
    }


def project_repositories(root: Path, name: str, *, enabled_only: bool = True) -> list[dict[str, Any]]:
    pp = ensure_project_layout(root, name)
    registry = project_repository_registry(root, name)
    rows: list[dict[str, Any]] = []
    for repo_id, raw in sorted(dict(registry.get("items") or {}).items()):
        item = dict(raw or {})
        if enabled_only and not bool(item.get("enabled", True)):
            continue
        rel = Path(str(item.get("path") or ""))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            continue
        absolute = (pp.project_dir / rel).resolve()
        try:
            absolute.relative_to(pp.project_dir.resolve())
        except ValueError:
            continue
        rows.append({
            "repo_id": repo_id,
            "path": rel.as_posix(),
            "root": absolute,
            "enabled": bool(item.get("enabled", True)),
            "default": repo_id == registry.get("default"),
            "legacy": bool(item.get("legacy", False)),
        })
    return rows


def repository_root_status(repo_root: Path) -> dict[str, Any]:
    """Expose passive repository-root inspection for MCP repository management."""
    return _repository_state_for_root(repo_root)


def resolve_project_repository(
    root: Path,
    name: str,
    repo_id: str = "",
    *,
    require_unique: bool = True,
) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    registry = project_repository_registry(root, name)
    rows = project_repositories(root, name)
    requested = str(repo_id or "").strip()
    if requested:
        requested = "default" if registry.get("mode") == "legacy" and requested == "default" else _clean_repo_id(requested)
        matches = [row for row in rows if row["repo_id"] == requested]
        if not matches:
            return {"status": "not_found", "project_id": pp.project_id, "repo_id": requested, "reason": "repository is not registered or enabled"}
        return {"status": "ok", "project_id": pp.project_id, **matches[0], "registry_mode": registry.get("mode")}
    default_id = str(registry.get("default") or "")
    if default_id:
        defaults = [row for row in rows if row["repo_id"] == default_id]
        if defaults and (not require_unique or len(rows) <= 1):
            return {"status": "ok", "project_id": pp.project_id, **defaults[0], "registry_mode": registry.get("mode")}
    if len(rows) == 1:
        return {"status": "ok", "project_id": pp.project_id, **rows[0], "registry_mode": registry.get("mode")}
    if not rows:
        return {"status": "not_found", "project_id": pp.project_id, "reason": "project has no enabled repositories"}
    if require_unique:
        return {
            "status": "ambiguous_repository",
            "project_id": pp.project_id,
            "reason": "multiple repositories are registered; provide repo= explicitly",
            "repositories": [{k: v for k, v in row.items() if k != "root"} for row in rows],
        }
    if default_id:
        defaults = [row for row in rows if row["repo_id"] == default_id]
        if defaults:
            return {"status": "ok", "project_id": pp.project_id, **defaults[0], "registry_mode": registry.get("mode")}
    return {"status": "ok", "project_id": pp.project_id, **rows[0], "registry_mode": registry.get("mode")}


def project_repo_add(root: Path, name: str, repo_id: str, path: str, *, default: bool = False) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    rid = _clean_repo_id(repo_id)
    explicit_sources = dict((_meta(pp).get("sources") or {}).get("items") or {})
    if rid in explicit_sources:
        raise ValueError("repository id conflicts with a registered non-Git source id")
    rel = Path(str(path or "").strip().replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("repository path must be project-relative")
    if rel == Path("repo"):
        raise ValueError("registered repositories must be children of repo/, e.g. repo/oathkeeper")
    if not rel.parts or rel.parts[0] != "repo":
        raise ValueError("registered repository path must live under project repo/")
    absolute = (pp.project_dir / rel).resolve()
    try:
        absolute.relative_to((pp.project_dir / "repo").resolve())
    except ValueError as exc:
        raise ValueError("registered repository path must stay under project repo/") from exc
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        repositories = dict(meta.get("repositories") or {})
        items = dict(repositories.get("items") or {})
        items[rid] = {"path": rel.as_posix(), "enabled": True}
        repositories["mode"] = "registered"
        repositories["items"] = items
        if default or not str(repositories.get("default") or "") or str(repositories.get("default")) == "default":
            repositories["default"] = rid
        meta["repositories"] = repositories
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {"status": "registered", "project_id": pp.project_id, "repo_id": rid, "path": rel.as_posix(), "default": str(project_repository_registry(root, name).get("default")) == rid}


def project_repo_remove(root: Path, name: str, repo_id: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    rid = _clean_repo_id(repo_id)
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        repositories = dict(meta.get("repositories") or {})
        if str(repositories.get("mode") or "legacy") != "registered":
            raise ValueError("legacy default repository cannot be removed; register child repositories first")
        items = dict(repositories.get("items") or {})
        existed = rid in items
        items.pop(rid, None)
        repositories["items"] = items
        if str(repositories.get("default") or "") == rid:
            repositories["default"] = next(iter(sorted(items)), "")
        # Deliberately keep registered mode when empty: falling back to legacy
        # would reinterpret the repo/ container as one repository.
        repositories["mode"] = "registered"
        meta["repositories"] = repositories
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {"status": "removed" if existed else "not_found", "project_id": pp.project_id, "repo_id": rid, "default": project_repository_registry(root, name).get("default", "")}


def project_repo_default(root: Path, name: str, repo_id: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    registry = project_repository_registry(root, name)
    rid = "default" if registry.get("mode") == "legacy" and repo_id == "default" else _clean_repo_id(repo_id)
    if rid not in dict(registry.get("items") or {}):
        raise ValueError(f"unknown repository: {rid}")
    if registry.get("mode") == "legacy":
        return {"status": "already_default", "project_id": pp.project_id, "repo_id": "default"}
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        repositories = dict(meta.get("repositories") or {})
        repositories["default"] = rid
        meta["repositories"] = repositories
        meta["updated_at"] = now_ts()
        _write_json(pp.project_json, meta)
    return {"status": "default_set", "project_id": pp.project_id, "repo_id": rid}

def project_exists(root: Path, name: str) -> bool:
    return paths_for(root, name).project_json.exists()


def _meta(pp: ProjectPaths) -> dict[str, Any]:
    return _upgrade_meta(_read_json(pp.project_json), pp.project_id)


def enable_code_index(root: Path, name: str) -> dict[str, Any]:
    """Persist explicit consent to index the project's repository source.

    Code indexing remains off in the default project template. Calling the
    dedicated codebase workflow is the explicit action that enables it.
    """
    pp = ensure_project_layout(root, name)
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        rag = dict(meta.get("rag") or {})
        changed = not bool(rag.get("index_code", False))
        rag["index_code"] = True
        meta["rag"] = rag
        if changed:
            meta["updated_at"] = now_ts()
            _write_json(pp.project_json, meta)
    return {
        "status": "enabled" if changed else "already_enabled",
        "project_id": pp.project_id,
        "index_code": True,
    }


def _record_timestamp(record: Mapping[str, Any]) -> str:
    return str(record.get("timestamp") or record.get("created_at") or "")


def _bump_workspace(pp: ProjectPaths, record: Mapping[str, Any]) -> None:
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        continuity_meta = meta["continuity"]
        continuity_meta["workspace_generation"] = int(continuity_meta.get("workspace_generation") or 0) + 1
        continuity_meta["last_record_id"] = str(record.get("id") or "")
        continuity_meta["last_record_at"] = _record_timestamp(record) or now_ts()
        meta["updated_at"] = continuity_meta["last_record_at"]
        _write_json(pp.project_json, meta)


def _update_attached_sessions_after_capture(root: Path, project_id: str, record_id: str) -> None:
    base = sessions_dir(root)
    if not base.exists() or not record_id:
        return
    for state_path in base.glob("*.json"):
        with _state_lock(state_path):
            state = _read_json(state_path)
            if state.get("status") != "active" or state.get("project_id") != project_id:
                continue
            state["last_capture_id"] = record_id
            state["last_activity_at"] = now_ts()
            _write_json(state_path, state)


def register_appended_record(root: Path, name: str, record: Mapping[str, Any]) -> None:
    """Advance project/session generations for a record appended externally."""
    pp = ensure_project_layout(root, name)
    _bump_workspace(pp, record)
    _update_attached_sessions_after_capture(root, pp.project_id, str(record.get("id") or ""))


def _run_capture_hook(root: Path, project_id: str, saved: Mapping[str, Any], *, views_refreshed: bool) -> dict[str, Any] | None:
    if _CAPTURE_HOOK is None or saved.get("_write_status") != "appended":
        return None
    try:
        result = _CAPTURE_HOOK(root, project_id, {**saved, "_views_refreshed": views_refreshed})
        return dict(result) if isinstance(result, Mapping) else None
    except Exception as exc:  # pragma: no cover - backend/environment dependent
        return {"status": "warning", "reason": f"capture_hook_failed:{exc}"}


def project_capture(
    root: Path,
    name: str,
    summary: str,
    *,
    details: str = "",
    kind: str = "observation",
    sources: Iterable[Any] | None = None,
    confidence: str = "medium",
    sensitivity: str = "project",
    index_policy: str = "safe",
    tags: Iterable[str] | None = None,
    uncertainty: Iterable[str] | None = None,
    likely_continuation: str = "",
    supersedes: Iterable[str] | None = None,
    state: str = "",
    metadata: Mapping[str, Any] | None = None,
    refresh: bool = True,
    sync_index: bool = True,
    allow_sensitive_plaintext: bool = False,
) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    record = continuity.make_record(
        pp.project_id,
        summary,
        kind=kind,
        details=details,
        sources=sources,
        confidence=confidence,
        sensitivity=sensitivity,
        index_policy=index_policy,
        tags=tags,
        uncertainty=uncertainty,
        likely_continuation=likely_continuation,
        supersedes=supersedes,
        state=state,
        metadata=metadata,
        allow_sensitive_plaintext=allow_sensitive_plaintext,
    )
    saved = continuity.append_record(pp.continuity, record)
    if saved.get("_write_status") == "appended":
        _bump_workspace(pp, saved)
        _update_attached_sessions_after_capture(root, pp.project_id, str(saved.get("id") or ""))
    if refresh:
        refresh_project_files(root, pp.project_id)
    hook_result = _run_capture_hook(root, pp.project_id, saved, views_refreshed=refresh) if sync_index else None
    if hook_result:
        saved = {**saved, "exact_index_sync": hook_result}
    return saved


def continuity_records(pp: ProjectPaths, include_legacy: bool = True) -> list[dict[str, Any]]:
    return continuity.active_records(continuity.load_records(pp.memory_dir, pp.project_id, include_legacy=include_legacy))



def view_safe_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Exclude explicit sensitive/no-RAG records from generated views and automatic resume packs."""
    return [
        dict(record)
        for record in records
        if str(record.get("index_policy") or "safe").lower() != "no_rag"
        and str(record.get("sensitivity") or "project").lower() not in {"sensitive", "secret"}
    ]

def resolved_pending_ids(pp: ProjectPaths) -> set[str]:
    return {
        str(row["id"])
        for row in read_jsonl(pp.memory_dir / "pending.jsonl")
        if row.get("kind") == "pending_resolution" and row.get("id")
    }


def pending_items(pp: ProjectPaths, include_done: bool = False) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(pp.memory_dir / "pending.jsonl") if row.get("kind") == "pending"]
    if include_done:
        return rows
    resolved = resolved_pending_ids(pp)
    return [row for row in rows if str(row.get("id")) not in resolved and str(row.get("status", "pending")) in {"pending", "in_progress", "blocked"}]


def burp_run_pointers(pp: ProjectPaths, limit: int = 3) -> list[dict[str, Any]]:
    rows = read_jsonl(pp.artifacts_dir / "burp" / "runs.jsonl")
    rows.sort(key=lambda row: str(row.get("created_at") or row.get("run_id", "")), reverse=True)
    return rows[:limit]


def _safe_workspace_materials(pp: ProjectPaths, limit: int = 24) -> list[str]:
    """Return bounded safe project-relative material paths for generated views."""
    roots = [pp.notes_dir, pp.project_dir / "reports", pp.corpora_dir, pp.artifacts_dir]
    registered = indexing_policy.read_safe_artifact_registry(pp.index_dir)
    rows: list[str] = []
    for base in roots:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*"), key=lambda value: value.as_posix()):
            if not path.is_file() or path.is_symlink() or path.name == "README.md":
                continue
            try:
                project_rel = path.relative_to(pp.project_dir)
                root_rel = path.relative_to(pp.root)
            except ValueError:
                continue
            decision = indexing_policy.decide_file(
                path,
                rel=root_rel,
                category="analysis",
                redact=safety.redact_analysis_text,
                registered_safe=registered,
                strict_artifacts=project_rel.parts[:1] == ("artifacts",),
            )
            if decision.included:
                rows.append(project_rel.as_posix())
            if len(rows) >= limit:
                return rows
    return rows


def _safe_git_status_path(value: str) -> str:
    """Normalize a Git status path without hiding security-relevant filenames.

    Git status exposes path names, not file contents. Names such as `.env`,
    `credentials.json`, `auth/`, or `secrets/` are important project-state
    evidence and must not disappear merely because the underlying file may hold
    sensitive values.
    """
    text = value.strip().replace("\\", "/")
    if " -> " in text:
        text = text.split(" -> ", 1)[-1]
    candidate = Path(text)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return ""
    return candidate.as_posix()


def _repository_state_for_root(repo: Path) -> dict[str, Any]:
    if not repo.exists():
        return {"present": False}
    try:
        git_env = _passive_git_env()
        top = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=git_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        top = None
    if top is None or top.returncode != 0:
        count = sum(1 for path in repo.rglob("*") if path.is_file() and not path.is_symlink())
        return {"present": True, "git": False, "file_count": count, "configured_root": str(repo)}
    try:
        git_root = Path(top.stdout.strip()).resolve()
        exact_root = git_root == repo.resolve()
    except OSError:
        git_root = Path(top.stdout.strip() or ".")
        exact_root = False
    if not exact_root:
        return {
            "present": True,
            "git": True,
            "invalid_repo_root": True,
            "configured_root": str(repo),
            "git_root": str(git_root),
            "dirty": True,
            "changed_paths": [],
        }

    def git_value(*args: str) -> str:
        try:
            env = _passive_git_env()
            proc = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout.rstrip("\r\n") if proc.returncode == 0 else ""

    branch = git_value("branch", "--show-current") or "detached"
    head = git_value("rev-parse", "--short=12", "HEAD")
    configured_filter_keys = git_value("config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$")
    filter_names: set[str] = set()
    for key in configured_filter_keys.splitlines():
        match = re.match(r"^filter\.(.+)\.(?:clean|smudge|process|required)$", key.strip())
        if match and match.group(1):
            filter_names.add(match.group(1))
    status_config: list[str] = []
    for filter_name in sorted(filter_names):
        status_config.extend([
            "-c", f"filter.{filter_name}.process=",
            "-c", f"filter.{filter_name}.clean=",
            "-c", f"filter.{filter_name}.smudge=",
            "-c", f"filter.{filter_name}.required=false",
        ])
    status_text = git_value(*status_config, "status", "--porcelain=v1", "--untracked-files=normal")
    changed: list[str] = []
    for line in status_text.splitlines():
        clean = _safe_git_status_path(line[3:] if len(line) > 3 else line)
        if clean and clean not in changed:
            changed.append(clean)
    return {
        "present": True,
        "git": True,
        "configured_root": str(repo),
        "branch": branch,
        "head": head,
        "dirty": bool(status_text),
        "cleanliness_proof": "literal_worktree_index_no_filters" if filter_names else "git_status",
        "configured_filter_drivers": sorted(filter_names)[:20],
        "changed_paths": sorted(changed)[:20],
        "hidden_changed_path_count": max(0, len(status_text.splitlines()) - len(changed)),
    }


def _repository_state(pp: ProjectPaths) -> dict[str, Any]:
    # This path is called while continuity views may already hold the project
    # state lock. It must remain read-only and never call ensure_project_layout.
    meta = _meta(pp)
    registry = dict(meta.get("repositories") or {})
    mode = str(registry.get("mode") or "legacy")
    if mode == "legacy":
        return _repository_state_for_root(pp.project_dir / "repo")
    default_id = str(registry.get("default") or "")
    repositories: list[dict[str, Any]] = []
    for repo_id, raw in sorted(dict(registry.get("items") or {}).items()):
        item = dict(raw or {})
        if not bool(item.get("enabled", True)):
            continue
        rel = Path(str(item.get("path") or ""))
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            continue
        absolute = (pp.project_dir / rel).resolve()
        try:
            absolute.relative_to(pp.project_dir.resolve())
        except ValueError:
            continue
        state = _repository_state_for_root(absolute)
        repositories.append({
            "repo_id": str(repo_id),
            "path": rel.as_posix(),
            "default": str(repo_id) == default_id,
            **state,
        })
    return {
        "present": bool(repositories),
        "multi_repo": True,
        "registry_mode": "registered",
        "repositories": repositories,
    }


def _repository_state_lines(pp: ProjectPaths) -> list[str]:
    state = _repository_state(pp)
    if state.get("multi_repo"):
        rows = list(state.get("repositories") or [])
        if not rows:
            return ["- No enabled repositories are registered."]
        lines = [f"- registered repositories: {len(rows)}"]
        for row in rows:
            rid = str(row.get("repo_id") or "unknown")
            marker = " (default)" if row.get("default") else ""
            if not row.get("present"):
                lines.append(f"- `{rid}`{marker}: `{row.get('path')}` is missing.")
            elif not row.get("git"):
                lines.append(f"- `{rid}`{marker}: `{row.get('path')}` is not a Git worktree ({row.get('file_count', 0)} files).")
            elif row.get("invalid_repo_root"):
                lines.append(f"- `{rid}`{marker}: configured root `{row.get('path')}` is not the exact Git top-level (`{row.get('git_root')}`).")
            else:
                lines.append(
                    f"- `{rid}`{marker}: branch `{row.get('branch') or 'unknown'}`, HEAD `{row.get('head') or 'unborn'}`, dirty {str(bool(row.get('dirty'))).lower()}."
                )
                for changed in row.get("changed_paths", []):
                    lines.append(f"  - changed: `{row.get('path')}/{changed}`")
        return lines
    if not state.get("present"):
        return []
    if not state.get("git"):
        return [f"- `repo/` is present but is not a Git worktree ({state.get('file_count', 0)} files)."]
    if state.get("invalid_repo_root"):
        return [
            "- `repo/` is not the exact Git worktree root; code evidence must not be bound to this checkout.",
            f"- configured repo: `{state.get('configured_root')}`",
            f"- detected Git root: `{state.get('git_root')}`",
        ]
    lines = [
        f"- branch: `{state.get('branch') or 'unknown'}`",
        f"- HEAD: `{state.get('head') or 'unborn'}`",
        f"- dirty: {str(bool(state.get('dirty'))).lower()}",
    ]
    if state.get("cleanliness_proof") == "literal_worktree_index_no_filters":
        lines.append("- configured Git content-filter helpers were neutralized; cleanliness is based on literal worktree/index comparison and may be conservative.")
    lines.extend(f"- changed: `repo/{path}`" for path in state.get("changed_paths", []))
    if state.get("hidden_changed_path_count"):
        lines.append(f"- {state['hidden_changed_path_count']} malformed changed path(s) could not be represented safely.")
    return lines


def _workspace_inventory(pp: ProjectPaths) -> list[str]:
    labels: list[str] = []
    checks = [
        (pp.project_dir / "repo", "a repository"),
        (pp.notes_dir, "notes"),
        (pp.corpora_dir, "reference corpora"),
        (pp.artifacts_dir, "artifacts"),
        (pp.project_dir / "reports", "reports"),
    ]
    for directory, label in checks:
        if not directory.exists():
            continue
        if any(p.is_file() and p.name != "README.md" for p in directory.rglob("*")):
            labels.append(label)
    return labels


def _project_narrative(pp: ProjectPaths, meta: Mapping[str, Any], records: list[dict[str, Any]]) -> str:
    description = str(meta.get("description") or "").strip()
    if description:
        return description
    inventory = _workspace_inventory(pp)
    recent = continuity.meaningful_records(records)[-3:]
    pieces = []
    if inventory:
        pieces.append("The workspace contains " + ", ".join(inventory) + ".")
    if recent:
        pieces.append("Recent continuity centers on " + "; ".join(str(r.get("summary")) for r in recent) + ".")
    return " ".join(pieces) or "This is an active free-form project workspace. No single goal is assumed."


def rag_policy_hash(meta: Mapping[str, Any]) -> str:
    policy = meta.get("rag") if isinstance(meta.get("rag"), Mapping) else {}
    payload = json.dumps(dict(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _index_freshness(pp: ProjectPaths, meta: Mapping[str, Any]) -> dict[str, Any]:
    manifest = indexing_policy.read_index_manifest(pp.index_manifest)
    workspace_generation = int((meta.get("continuity") or {}).get("workspace_generation") or 0)
    indexed_generation = int(manifest.get("workspace_generation") or 0)
    current_probe = workspace_index_probe(
        pp,
        include_artifacts=bool(manifest.get("include_artifacts", True)),
        include_code=bool(manifest.get("general_include_code", manifest.get("include_code", False))),
        include_generated=False,
    ) if manifest else {"hash": "", "file_count": 0}
    indexed_probe_hash = str(manifest.get("source_probe_hash") or manifest.get("workspace_probe_hash") or "")
    probe_matches = bool(indexed_probe_hash) and indexed_probe_hash == current_probe["hash"]
    current_policy_hash = rag_policy_hash(meta)
    indexed_policy_hash = str(manifest.get("project_policy_hash") or "")
    policy_matches = bool(indexed_policy_hash) and indexed_policy_hash == current_policy_hash
    current = bool(manifest) and indexed_generation >= workspace_generation and probe_matches and policy_matches
    return {
        "workspace_generation": workspace_generation,
        "indexed_generation": indexed_generation,
        # ``fresh`` remains as a compatibility alias.  The explicit field name
        # prevents this project-memory/general-RAG projection from being confused
        # with the independent structural code-index freshness reported by the
        # code_* tools.
        "fresh": current,
        "project_memory_index_current": current,
        "freshness_scope": "project_memory_general_rag_projection",
        "does_not_describe": "structural_code_index_freshness",
        "workspace_probe_hash": current_probe["hash"],
        "indexed_probe_hash": indexed_probe_hash,
        "probe_matches": probe_matches,
        "project_policy_hash": current_policy_hash,
        "indexed_project_policy_hash": indexed_policy_hash,
        "policy_matches": policy_matches,
        "workspace_probe_file_count": current_probe["file_count"],
        "indexed_at": manifest.get("indexed_at", ""),
        "included_count": len(manifest.get("included", [])) if isinstance(manifest.get("included"), list) else 0,
        "excluded_count": len(manifest.get("excluded", [])) if isinstance(manifest.get("excluded"), list) else 0,
    }


def workspace_index_probe(
    pp: ProjectPaths,
    *,
    include_artifacts: bool,
    include_code: bool,
    include_generated: bool = True,
) -> dict[str, Any]:
    """Compute a cheap conservative fingerprint of index-relevant workspace files.

    The probe intentionally errs toward *extra* refreshes instead of missing an
    externally edited security-relevant file. It never suppresses a path merely
    because it is named `.env`, `credentials`, `auth`, `secrets`, `build`, etc.
    Repository code probing also includes unknown textual formats and lexical-only
    source candidates so general project freshness cannot lag behind code coverage.
    """
    roots: list[tuple[Path, str]] = [
        (pp.notes_dir / "thoughts.md", "analysis"),
        (pp.project_dir / "README.md", "analysis"),
        (pp.project_dir / "AGENTS.md", "analysis"),
        (pp.index_dir / "safe_artifacts.jsonl", "analysis"),
    ]
    if include_generated:
        roots[0:0] = [(pp.situation, "analysis"), (pp.handoff, "analysis")]
    if include_artifacts:
        roots.extend([
            (pp.corpora_dir, "analysis"),
            (pp.project_dir / "reports", "analysis"),
            (pp.artifacts_dir, "analysis"),
        ])
    if include_code:
        roots.extend([
            (pp.project_dir / "repo", "code"),
            (pp.corpora_dir / "code", "code"),
        ])

    entries: list[str] = []
    seen: set[Path] = set()
    for root, domain in roots:
        candidates = [root] if root.is_file() else root.rglob("*") if root.exists() else []
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                rel = path.relative_to(pp.project_dir)
                lowered = {part.lower() for part in rel.parts}
                if domain == "analysis":
                    if path.is_symlink() or not path.is_file():
                        continue
                    if lowered & indexing_policy.ANALYSIS_NEVER_INDEX_PARTS:
                        continue
                    if (
                        path.suffix.lower() not in indexing_policy.SAFE_TEXT_SUFFIXES
                        and not indexing_policy.is_explicit_sensitive_path(path)
                        and path != pp.index_dir / "safe_artifacts.jsonl"
                    ):
                        continue
                    stat = path.stat()
                    if stat.st_size > 2_000_000:
                        continue
                else:
                    # Code coverage treats a symlink itself as relevant state
                    # because it changes repository completeness even though the
                    # target is not followed for indexing.
                    if ".git" in lowered:
                        continue
                    if path.is_symlink():
                        stat = path.lstat()
                    elif path.is_file():
                        stat = path.stat()
                        try:
                            with path.open("rb") as handle:
                                sample = handle.read(65536)
                        except OSError:
                            continue
                        if not indexing_policy.looks_textual_bytes(sample):
                            continue
                    else:
                        continue
            except (OSError, ValueError):
                continue
            entries.append(f"{rel.as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}")
    entries.sort()
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"hash": digest, "file_count": len(entries)}


def _records_since(records: list[dict[str, Any]], record_id: str) -> list[dict[str, Any]]:
    if not record_id:
        return records[-12:]
    for index, record in enumerate(records):
        if str(record.get("id")) == record_id:
            return records[index + 1:]
    return records[-12:]


def _knowledge_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = [r for r in records if r.get("kind") in continuity.KNOWLEDGE_KINDS]
    high = [r for r in rows if r.get("confidence") == "high"]
    others = [r for r in rows if r.get("confidence") != "high"]
    ordered = high + others
    return ordered[-limit:]


def _uncertainties(records: list[dict[str, Any]], limit: int = 10) -> list[str]:
    out: list[str] = []
    for record in reversed(records):
        if record.get("kind") in continuity.UNCERTAINTY_KINDS:
            value = str(record.get("summary") or "").strip()
            if value and value not in out:
                out.append(value)
        for value in record.get("uncertainty", []) or []:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
        if len(out) >= limit:
            break
    return out


def _continuations(records: list[dict[str, Any]], pp: ProjectPaths, limit: int = 8) -> list[str]:
    """Return optional continuation ideas with explicit user direction first.

    Pending facets and inferred next steps are useful context, but a later
    direction record represents the user's current instruction and must take
    precedence over every older suggestion.
    """
    out: list[str] = []
    closed_states = {"done", "closed", "resolved", "superseded"}

    for record in reversed(records):
        if record.get("kind") != "direction" or str(record.get("state") or "").lower() in closed_states:
            continue
        for value in (record.get("summary"), record.get("likely_continuation")):
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
        if out:
            break

    for item in pending_items(pp):
        value = str(item.get("next_action") or item.get("title") or "").strip()
        if value and value not in out:
            out.append(value)

    for record in reversed(records):
        if record.get("kind") == "direction" or str(record.get("state") or "").lower() in closed_states:
            continue
        for value in (
            record.get("likely_continuation"),
            record.get("summary") if record.get("kind") in continuity.CONTINUATION_KINDS else "",
        ):
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
        if len(out) >= limit:
            break
    return out[:limit]


def _section(title: str, lines: Iterable[str]) -> list[str]:
    clean = [line for line in lines if str(line).strip()]
    if not clean:
        return []
    return [f"## {title}", "", *clean, ""]


def _bounded_markdown(text: str, max_chars: int) -> str:
    """Return deterministic, line-bounded markdown within a context budget."""
    normalized = text.rstrip() + "\n"
    if len(normalized) <= max_chars:
        return normalized
    marker = "\n_Additional generated detail omitted to preserve the continuity context budget._\n"
    cutoff = max(0, max_chars - len(marker))
    boundary = normalized.rfind("\n", 0, cutoff)
    if boundary < max(0, cutoff // 2):
        boundary = cutoff
    return normalized[:boundary].rstrip() + marker


def _render_situation(pp: ProjectPaths, meta: Mapping[str, Any], records: list[dict[str, Any]]) -> str:
    recent = continuity.meaningful_records(records)[-8:]
    knowledge = _knowledge_records(records, 8)
    uncertainties = _uncertainties(records, 6)
    sources = continuity.unique_sources(records, 8)
    materials = _safe_workspace_materials(pp, 12)
    repository = _repository_state_lines(pp)
    continuations = _continuations(records, pp, 6)
    freshness = _index_freshness(pp, meta)
    generation = int((meta.get("continuity") or {}).get("workspace_generation") or 0)
    lines = [
        f"# Situation: {pp.project_id}",
        "",
        f"_Generated continuity snapshot · workspace generation {generation}_",
        "",
        *_section("Project at a glance", [_project_narrative(pp, meta, records)]),
        *_section("Recent meaningful changes", [continuity.record_line(r) for r in recent]),
        *_section("Important knowledge", [continuity.record_line(r) for r in knowledge]),
        *_section("Open uncertainty", [f"- {value}" for value in uncertainties]),
        *_section("Useful materials", [f"- {continuity.source_label(source)}" for source in sources] + [f"- `{path}`" for path in materials]),
        *_section("Repository state", repository),
        *_section("Possible continuations", [f"- {value}" for value in continuations] + ["- Continue in a different direction supplied by the user."]),
        "## Index freshness",
        "",
        "- scope: project memory/general RAG projection (not structural code-index freshness)",
        f"- workspace_generation: {freshness['workspace_generation']}",
        f"- indexed_generation: {freshness['indexed_generation']}",
        f"- project_memory_index_current: {str(freshness['project_memory_index_current']).lower()}",
        "",
    ]
    return _bounded_markdown("\n".join(lines), 12_000)


def _render_handoff(
    pp: ProjectPaths,
    meta: Mapping[str, Any],
    records: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> str:
    knowledge = _knowledge_records(records, 24)
    decisions = [r for r in records if r.get("kind") in {"decision", "correction"}][-12:]
    recent = continuity.meaningful_records(records)[-16:]
    uncertainties = _uncertainties(records, 15)
    sources = continuity.unique_sources(records, 24)
    materials = _safe_workspace_materials(pp, 24)
    repository = _repository_state_lines(pp)
    continuations = _continuations(records, pp, 10)
    burp_runs = burp_run_pointers(pp, 5)
    freshness = _index_freshness(pp, meta)
    generation = int((meta.get("continuity") or {}).get("workspace_generation") or 0)
    source_lines = [f"- {continuity.source_label(source)}" for source in sources]
    burp_lines = [
        f"- `{row.get('run_id', 'unknown')}` — {row.get('source_type', 'unknown')} — status={row.get('status', 'unknown')}"
        for row in burp_runs
    ]
    lines = [
        f"# Handoff: {pp.project_id}",
        "",
        f"_Generated resume document · workspace generation {generation}_",
        "",
        *_section("Project identity", [
            f"- project_id: {pp.project_id}",
            f"- status: {meta.get('status', 'active')}",
            f"- workspace: `{pp.project_dir}`",
        ]),
        *_section("Compact project narrative", [_project_narrative(pp, meta, records)]),
        *_section("Changes since the previous handoff", [continuity.record_line(r) for r in changes]),
        *_section("Important established knowledge", [continuity.record_line(r) for r in knowledge]),
        *_section("Key decisions and corrections", [continuity.record_line(r) for r in decisions]),
        *_section("Recent meaningful activity", [continuity.record_line(r) for r in recent]),
        *_section("Useful files, artifacts, and sources", source_lines + [f"- `{path}`" for path in materials]),
        *_section("Repository state", repository),
        *(_section("Related Burp evidence summaries", burp_lines) if burp_lines else []),
        *_section("Areas of uncertainty", [f"- {value}" for value in uncertainties]),
        *_section("Possible continuation points", [f"- {value}" for value in continuations] + ["- Follow the user's new direction instead of assuming any suggestion is mandatory."]),
        "## What should not be assumed",
        "",
        "- A single current goal, task list, or pending item exists.",
        "- A continuation suggestion is not an instruction; explicit user direction always overrides it.",
        "- Low-confidence or unresolved records are not established facts.",
        "- Global knowledge is not project-established knowledge unless a project source confirms it.",
        "- Raw artifacts and explicit sensitive memory are not loaded or indexed automatically; optional adapters keep their raw evidence outside generic recall.",
        "",
        "## Index state",
        "",
        "- scope: project memory/general RAG projection (not structural code-index freshness)",
        f"- workspace_generation: {freshness['workspace_generation']}",
        f"- indexed_generation: {freshness['indexed_generation']}",
        f"- project_memory_index_current: {str(freshness['project_memory_index_current']).lower()}",
        f"- included_documents: {freshness['included_count']}",
        f"- excluded_candidates: {freshness['excluded_count']}",
        "",
        "## Resume order",
        "",
        "1. Use this handoff and SITUATION.md for orientation.",
        "2. Read recent continuity reflections when more detail is needed.",
        "3. Use targeted project search for the current question.",
        "4. Open raw files or artifacts only when explicitly needed.",
        "",
    ]
    return _bounded_markdown("\n".join(lines), 32_000)


def _write_if_changed(path: Path, text: str) -> bool:
    old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
    if old == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def refresh_project_files(root: Path, name: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        all_records = continuity_records(pp)
        records = view_safe_records(all_records)
        previous_handoff_id = str(meta["continuity"].get("last_handoff_record_id") or "")
        changes = _records_since(records, previous_handoff_id)
        situation_text = _render_situation(pp, meta, records)
        handoff_text = _render_handoff(pp, meta, records, changes)
        situation_changed = _write_if_changed(pp.situation, situation_text)
        handoff_changed = _write_if_changed(pp.handoff, handoff_text)
        meta["continuity"]["snapshot_generation"] = int(meta["continuity"].get("workspace_generation") or 0)
        meta["continuity"]["last_snapshot_change_ids"] = [str(r.get("id")) for r in changes[-20:] if r.get("id")]
        _write_json(pp.project_json, meta)
        result = {
            "status": "refreshed",
            "project_id": pp.project_id,
            "situation": str(pp.situation),
            "handoff": str(pp.handoff),
            "situation_changed": situation_changed,
            "handoff_changed": handoff_changed,
            "workspace_generation": meta["continuity"]["workspace_generation"],
            "snapshot_generation": meta["continuity"]["snapshot_generation"],
            "record_count": len(all_records),
            "view_safe_record_count": len(records),
            "changes_since_previous_handoff": len(changes),
        }
    return result


def generated_view_audit(root: Path, name: str) -> dict[str, Any]:
    """Compute expected generated views without modifying project state."""
    pp = paths_for(root, name)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": pp.project_id}
    meta = _meta(pp)
    records = view_safe_records(continuity_records(pp))
    previous_handoff_id = str(meta["continuity"].get("last_handoff_record_id") or "")
    changes = _records_since(records, previous_handoff_id)
    expected_situation = _render_situation(pp, meta, records)
    expected_handoff = _render_handoff(pp, meta, records, changes)
    actual_situation = pp.situation.read_text(encoding="utf-8", errors="replace") if pp.situation.exists() else ""
    actual_handoff = pp.handoff.read_text(encoding="utf-8", errors="replace") if pp.handoff.exists() else ""
    return {
        "status": "audited",
        "project_id": pp.project_id,
        "situation_present": pp.situation.exists(),
        "handoff_present": pp.handoff.exists(),
        "situation_drift": actual_situation != expected_situation,
        "handoff_drift": actual_handoff != expected_handoff,
        "expected_situation_hash": hashlib.sha256(expected_situation.encode("utf-8")).hexdigest(),
        "actual_situation_hash": hashlib.sha256(actual_situation.encode("utf-8")).hexdigest(),
        "expected_handoff_hash": hashlib.sha256(expected_handoff.encode("utf-8")).hexdigest(),
        "actual_handoff_hash": hashlib.sha256(actual_handoff.encode("utf-8")).hexdigest(),
    }


def project_index_freshness(root: Path, name: str) -> dict[str, Any]:
    pp = paths_for(root, name)
    if not pp.project_json.exists():
        return {"fresh": False, "reason": "project_not_found", "project_id": pp.project_id}
    return _index_freshness(pp, _meta(pp))


def _acknowledge_resume(pp: ProjectPaths, latest_record_id: str) -> None:
    """Advance the handoff baseline only after a resume pack was produced.

    Generated views may refresh frequently. Treating every refresh as a consumed
    handoff erases the exact changes the next session needs to see.
    """
    with _state_lock(pp.project_json):
        meta = _meta(pp)
        meta["continuity"]["last_handoff_record_id"] = latest_record_id
        meta["continuity"]["last_resume_at"] = now_ts()
        _write_json(pp.project_json, meta)


def _capture_switch_activity(root: Path, previous: Mapping[str, Any], target_project: str) -> dict[str, Any] | None:
    activity = previous.get("activity") if isinstance(previous.get("activity"), dict) else {}
    if not activity.get("dirty"):
        return None
    project_id = str(previous.get("project_id") or "")
    if not project_id or not project_exists(root, project_id):
        return None
    changed_files = [str(item) for item in activity.get("changed_files") or []][:16]
    tools = [str(item) for item in activity.get("tools") or []][:16]
    file_count = int(activity.get("file_events") or 0)
    tool_count = int(activity.get("tool_events") or 0)
    details = [f"Atomic project switch to {target_project}."]
    if changed_files:
        details.append("Observed changed files: " + ", ".join(changed_files) + ".")
    if tools:
        details.append("Observed tools: " + ", ".join(tools) + ".")
    return project_capture(
        root,
        project_id,
        f"Continuity checkpoint before switching from {project_id} to {target_project}.",
        kind="continuity_reflection",
        details=" ".join(details),
        sources=[{"type": "file", "path": item} for item in changed_files],
        confidence="high" if changed_files else "medium",
        metadata={
            "capture_channel": "atomic_project_switch",
            "file_event_count": file_count,
            "tool_event_count": tool_count,
        },
        refresh=True,
    )


def attach_session_project(root: Path, name: str, session_id: str | None = None) -> dict[str, Any]:
    """Attach a project to one session, atomically preserving dirty prior activity."""
    pp = ensure_project_layout(root, name)
    meta = _meta(pp)
    sid = session_id or SESSION_ID
    state_path = session_state_path(root, sid)

    with _state_lock(state_path):
        observed = _read_json(state_path)
    same_project = observed.get("status") == "active" and observed.get("project_id") == pp.project_id
    switching_project = observed.get("status") == "active" and observed.get("project_id") and observed.get("project_id") != pp.project_id
    switch_checkpoint = _capture_switch_activity(root, observed, pp.project_id) if switching_project else None

    with _state_lock(state_path):
        previous = _read_json(state_path)
        if switching_project and (
            previous.get("status") != observed.get("status")
            or previous.get("project_id") != observed.get("project_id")
            or previous.get("activity") != observed.get("activity")
        ):
            return {
                "status": "switch_conflict",
                "session_id": sid,
                "current_project": previous.get("project_id"),
                "target_project": pp.project_id,
                "reason": "Session activity changed while the switch checkpoint was being written; retry the project_open operation.",
            }
        same_project = previous.get("status") == "active" and previous.get("project_id") == pp.project_id
        state = {
            "session_id": sid,
            "project_id": pp.project_id,
            "project_root": str(pp.project_dir),
            "opened_at": previous.get("opened_at") if same_project else now_ts(),
            "last_activity_at": now_ts(),
            "opened_workspace_generation": int(meta["continuity"].get("workspace_generation") or 0),
            "last_capture_id": str(meta["continuity"].get("last_record_id") or ""),
            "status": "active",
        }
        if same_project and isinstance(previous.get("activity"), dict):
            state["activity"] = previous["activity"]
        elif previous.get("status") == "active" and previous.get("project_id"):
            state["switched_from"] = previous.get("project_id")
        if switch_checkpoint:
            state["switch_checkpoint_id"] = str(switch_checkpoint.get("id") or "")
        # Detached-job continuation is session-scoped operational state. Project
        # attachment/switching must not erase it; scope conflicts are enforced by
        # the continuation scheduler/plugin instead of dropping the workflow.
        if isinstance(previous.get("continuation"), dict):
            state["continuation"] = previous["continuation"]
        _write_json(state_path, state)
    _write_json(legacy_session_state_path(root), {**state, "compatibility_only": True})
    _write_json(last_project_path(root), {k: v for k, v in state.items() if k != "session_id"} | {"last_seen_at": now_ts()})
    return state


def current_session_project(root: Path, session_id: str | None = None) -> dict[str, Any] | None:
    sid = session_id or SESSION_ID
    data = _read_json(session_state_path(root, sid))
    if not data or data.get("session_id") != sid or data.get("status") != "active":
        return None
    project_id = data.get("project_id")
    if not project_id or not project_exists(root, str(project_id)):
        return None
    return data


def current_project_id(root: Path, session_id: str | None = None) -> str | None:
    data = current_session_project(root, session_id=session_id)
    return str(data["project_id"]) if data else None


def detach_session_project(root: Path, name: str = "", session_id: str | None = None) -> dict[str, Any]:
    sid = session_id or SESSION_ID
    state_path = session_state_path(root, sid)
    with _state_lock(state_path):
        state = _read_json(state_path)
        if not state:
            return {"status": "not_attached"}
        if name and clean_project_id(name) != state.get("project_id"):
            return {"status": "not_attached", "project_id": clean_project_id(name)}
        state["status"] = "paused"
        state["paused_at"] = now_ts()
        state["last_activity_at"] = now_ts()
        _write_json(state_path, state)
    return {"status": "paused", "project_id": state.get("project_id"), "session_id": sid}


def _timestamp_age_seconds(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def session_recovery_plan(root: Path, *, stale_after_hours: float = 24.0) -> dict[str, Any]:
    threshold = max(0.0, float(stale_after_hours)) * 3600.0
    sessions: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    base = sessions_dir(root)
    if base.exists():
        for path in sorted(base.glob("*.json")):
            state = _read_json(path)
            if not state:
                invalid.append({"path": str(path), "reason": "invalid_json_or_empty"})
                continue
            age = _timestamp_age_seconds(str(state.get("last_activity_at") or state.get("opened_at") or ""))
            if state.get("status") != "active" or age is None or age < threshold:
                continue
            activity = state.get("activity") if isinstance(state.get("activity"), dict) else {}
            sessions.append({
                "path": str(path),
                "session_id": str(state.get("session_id") or ""),
                "project_id": str(state.get("project_id") or ""),
                "age_seconds": int(age),
                "last_activity_at": str(state.get("last_activity_at") or state.get("opened_at") or ""),
                "dirty": bool(activity.get("dirty")),
                "file_event_count": int(activity.get("file_events") or 0),
                "tool_event_count": int(activity.get("tool_events") or 0),
                "changed_files": [str(item) for item in activity.get("changed_files") or []][:16],
                "tools": [str(item) for item in activity.get("tools") or []][:16],
            })
    return {
        "status": "preview",
        "stale_after_hours": stale_after_hours,
        "stale_count": len(sessions),
        "invalid_count": len(invalid),
        "sessions": sessions,
        "invalid": invalid,
    }


def recover_stale_sessions(root: Path, *, stale_after_hours: float = 24.0, apply: bool = False) -> dict[str, Any]:
    plan = session_recovery_plan(root, stale_after_hours=stale_after_hours)
    if not apply:
        return plan
    recovered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in plan["sessions"]:
        sid = str(item.get("session_id") or "")
        project_id = str(item.get("project_id") or "")
        if not sid:
            skipped.append({**item, "reason": "missing_session_id"})
            continue
        if not project_id or not project_exists(root, project_id):
            result = recover_session_state(root, sid, reason="orphaned_stale_session")
            recovered.append({**item, "orphaned": True, "result": result})
            continue

        # Import lazily to keep the workspace layer independently importable and
        # to reuse the same lock-safe checkpoint/detach reconciliation as the
        # OpenCode plugin. The expected timestamp prevents a stale preview from
        # detaching a session that became active again before apply.
        import opencode_events
        result = opencode_events.checkpoint_session(
            root,
            sid,
            reason=f"stale_session_recovery:{stale_after_hours:g}h",
            detach=True,
            force=bool(item.get("dirty")),
            expected_last_activity_at=str(item.get("last_activity_at") or ""),
        )
        if result.get("status") in {"checkpoint_conflict", "state_changed", "not_attached"}:
            skipped.append({**item, "reason": result.get("status"), "result": result})
        else:
            recovered.append({**item, "result": result})
    return {
        "status": "recovered",
        "stale_after_hours": stale_after_hours,
        "recovered_count": len(recovered),
        "skipped_count": len(skipped),
        "recovered": recovered,
        "skipped": skipped,
        "invalid": plan["invalid"],
    }


def recover_session_state(root: Path, session_id: str, *, reason: str = "stale_session_recovery") -> dict[str, Any]:
    """Mark an unusable/orphaned session as recovered without deleting evidence."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"status": "rejected", "reason": "missing_session_id"}
    state_path = session_state_path(root, sid)
    with _state_lock(state_path):
        state = _read_json(state_path)
        if not state:
            return {"status": "not_found", "session_id": sid}
        state["status"] = "recovered"
        state["recovered_at"] = now_ts()
        state["recovery_reason"] = str(reason or "stale_session_recovery")[:500]
        state["last_activity_at"] = now_ts()
        _write_json(state_path, state)
    return {"status": "recovered", "session_id": sid, "project_id": state.get("project_id"), "path": str(state_path)}


def project_list(root: Path) -> list[dict[str, Any]]:
    base = projects_dir(root)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        meta = _read_json(path / "project.json")
        if meta:
            continuity_meta = meta.get("continuity") or {}
            out.append({
                "project_id": meta.get("project_id", path.name),
                "status": meta.get("status", "unknown"),
                "path": str(path),
                "updated_at": meta.get("updated_at", ""),
                "workspace_generation": continuity_meta.get("workspace_generation", 0),
            })
    return out


def project_create(root: Path, name: str, session_id: str | None = None) -> dict[str, Any]:
    existed = project_exists(root, name)
    pp = ensure_project_layout(root, name)
    if not existed:
        project_capture(
            root,
            pp.project_id,
            f"Project {pp.project_id} was created.",
            kind="event",
            confidence="high",
            refresh=False,
            sync_index=False,
        )
    session = attach_session_project(root, pp.project_id, session_id=session_id)
    refresh_project_files(root, pp.project_id)
    if session.get("status") == "switch_conflict":
        return {
            "status": session.get("status"),
            "project_id": pp.project_id,
            "created": not existed,
            "attached_for_current_session": False,
            "session": session,
        }
    return {**project_status(root, pp.project_id, session_id=session_id), "status": "created" if not existed else "already_exists", "attached_for_current_session": True}


def _resume_pack(pp: ProjectPaths) -> dict[str, Any]:
    meta = _meta(pp)
    records = continuity_records(pp)
    by_id = {str(r.get("id")): r for r in records}
    change_ids = meta["continuity"].get("last_snapshot_change_ids") or []
    changes = [by_id[str(record_id)] for record_id in change_ids if str(record_id) in by_id]
    recent_reflections = [r for r in records if r.get("kind") in {"reflection", "continuity_reflection"}][-6:]
    important = _knowledge_records(records, 12)
    sources = continuity.unique_sources(important + recent_reflections + changes, 16)
    uncertainties = _uncertainties(records, 10)
    continuations = _continuations(records, pp, 8)
    freshness = _index_freshness(pp, meta)
    return {
        "project_id": pp.project_id,
        "narrative": _project_narrative(pp, meta, records),
        "situation": pp.situation.read_text(encoding="utf-8", errors="replace")[:16_000],
        "handoff": pp.handoff.read_text(encoding="utf-8", errors="replace")[:32_000],
        "changes_since_previous_handoff": changes,
        "recent_reflections": recent_reflections,
        "important_knowledge": important,
        "sources": sources,
        "uncertainties": uncertainties,
        "possible_continuations": continuations,
        "index_freshness": freshness,
        "context_policy": {
            "order": ["situation", "handoff", "recent_reflections", "targeted_project_search", "explicit_raw_open"],
            "user_direction_overrides_continuation": True,
            "raw_artifacts_loaded_automatically": False,
        },
    }


def project_resume(root: Path, name: str, session_id: str | None = None) -> dict[str, Any]:
    pp = paths_for(root, name)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": pp.project_id, "reason": "Project does not exist. Create it explicitly or use project_open(create_if_missing=true)."}
    session = attach_session_project(root, pp.project_id, session_id=session_id)
    if session.get("status") == "switch_conflict":
        return {
            "status": session.get("status"),
            "project_id": pp.project_id,
            "attached_for_current_session": False,
            "session": session,
        }
    refresh_project_files(root, pp.project_id)
    pack = _resume_pack(pp)
    pack["suggested_next_action"] = pack["possible_continuations"][0] if pack["possible_continuations"] else "No continuation is implied; follow the user's current direction."
    pack["next_action"] = pack["suggested_next_action"]  # compatibility alias
    pack["next_action_is_suggestion"] = True
    pack["user_direction_overrides_suggestion"] = True
    records = continuity_records(pp)
    latest_id = str(records[-1].get("id") or "") if records else ""
    _acknowledge_resume(pp, latest_id)
    return {"status": "resumed", "attached_for_current_session": True, "session": session, **pack}


def project_open(root: Path, name: str, create_if_missing: bool = False, session_id: str | None = None) -> dict[str, Any]:
    if project_exists(root, name):
        return project_resume(root, name, session_id=session_id)
    if not create_if_missing:
        project_id = clean_project_id(name)
        return {"status": "not_found", "project_id": project_id, "reason": "Project creation is explicit. Retry with create_if_missing=true."}
    return project_create(root, name, session_id=session_id)


def project_summary(root: Path, name: str) -> str:
    pp = paths_for(root, name)
    if not pp.project_json.exists():
        return f"Project {pp.project_id} not found."
    records = continuity_records(pp)
    kinds: dict[str, int] = {}
    for record in records:
        kind = str(record.get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    counts = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())) or "no continuity records"
    return f"{pp.project_id}: {counts}; optional_pending={len(pending_items(pp))}."


def project_status(root: Path, name: str = "", session_id: str | None = None) -> dict[str, Any]:
    project_id = clean_project_id(name) if name else current_project_id(root, session_id=session_id)
    if not project_id:
        return {"status": "not_attached", "reason": "No project is attached to this session."}
    pp = paths_for(root, project_id)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": pp.project_id}
    refresh = refresh_project_files(root, pp.project_id)
    meta = _meta(pp)
    freshness = _index_freshness(pp, meta)
    warnings: list[str] = []
    if not freshness["fresh"]:
        warnings.append("Project index is stale or has not been built for the current workspace generation.")
    parse_errors = [r for r in continuity_records(pp) if r.get("kind") == "parse_error"]
    if parse_errors:
        warnings.append(f"Continuity contains {len(parse_errors)} parse error(s).")
    session = current_session_project(root, session_id=session_id)
    manifest = indexing_policy.read_index_manifest(pp.index_manifest)
    rag_policy = dict(meta.get("rag") or {})
    index_backends = dict(manifest.get("backends") or {}) if isinstance(manifest, dict) else {}
    return {
        "status": "ok",
        "project_id": pp.project_id,
        "path": str(pp.project_dir),
        "attached_for_current_session": current_project_id(root, session_id=session_id) == pp.project_id,
        "session": session,
        "summary": project_summary(root, pp.project_id),
        "situation_path": str(pp.situation),
        "handoff_path": str(pp.handoff),
        "continuity_path": str(pp.continuity),
        "optional_pending": pending_items(pp)[:10],
        "pending": pending_items(pp)[:10],
        "index_freshness": freshness,
        "index_policy": rag_policy,
        "index_backends": index_backends,
        "index_manifest_path": str(pp.index_manifest),
        "excluded_index_candidates": [
            {key: item.get(key) for key in ("path", "record_id", "kind", "reason")}
            for item in (manifest.get("excluded", []) if isinstance(manifest, dict) else [])[:20]
            if isinstance(item, dict)
        ],
        "warnings": warnings,
        "refresh": refresh,
    }


def project_handoff(root: Path, name: str) -> dict[str, Any]:
    pp = paths_for(root, name)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": pp.project_id}
    refresh = refresh_project_files(root, pp.project_id)
    return {"status": "handoff_ready", **_resume_pack(pp), "refresh": refresh}


def project_note(root: Path, name: str, text: str) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    note = text.strip()
    with (pp.notes_dir / "thoughts.md").open("a", encoding="utf-8") as f:
        f.write(f"\n## {now_ts()}\n\n{note}\n")
    captured = project_capture(
        root,
        pp.project_id,
        note[:2_000],
        kind="conversation_note",
        sources=[{"type": "file", "path": "notes/thoughts.md"}],
        confidence="high",
        refresh=True,
    )
    return {"status": "saved", "project_id": pp.project_id, "path": str(pp.notes_dir / "thoughts.md"), "continuity": captured}


def project_pending(root: Path, name: str, title: str, next_action: str, reason: str = "", related_files: list[str] | None = None) -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    pending_id = f"pending_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{uuid.uuid4().hex[:6]}"
    captured = project_capture(
        root,
        pp.project_id,
        title or next_action,
        kind="possible_continuation",
        details=reason,
        sources=related_files or [],
        confidence="medium",
        likely_continuation=next_action,
        state="open",
        metadata={"compatibility_pending_id": pending_id},
        refresh=False,
    )
    obj = append_jsonl(pp.memory_dir / "pending.jsonl", {
        "kind": "pending",
        "id": pending_id,
        "continuity_id": captured.get("id"),
        "status": "pending",
        "title": title,
        "reason": reason,
        "next_action": next_action,
        "related_files": related_files or [],
        "updated_at": now_ts(),
    })
    refresh_project_files(root, pp.project_id)
    return {"status": "queued", "project_id": pp.project_id, "pending": obj, "continuity": captured, "compatibility": "pending items are optional continuation facets"}


def project_mark_pending(root: Path, name: str, pending_id: str = "", status: str = "done", note: str = "") -> dict[str, Any]:
    pp = ensure_project_layout(root, name)
    items = pending_items(pp, include_done=False)
    target = next((p for p in items if p.get("id") == pending_id), None) if pending_id else None
    target = target or (items[0] if items else None)
    if not target:
        return {"status": "none", "project_id": pp.project_id, "reason": "No pending item found."}
    obj = append_jsonl(pp.memory_dir / "pending.jsonl", {
        "kind": "pending_resolution",
        "id": target.get("id"),
        "status": status,
        "note": note,
        "resolved_at": now_ts(),
    })
    captured = project_capture(
        root,
        pp.project_id,
        f"Continuation '{target.get('title') or target.get('next_action')}' was marked {status}.",
        kind="event",
        details=note,
        confidence="high",
        supersedes=[str(target.get("continuity_id"))] if target.get("continuity_id") else [],
        state=status,
        refresh=True,
    )
    return {"status": "marked", "project_id": pp.project_id, "pending_id": target.get("id"), "new_status": status, "resolution": obj, "continuity": captured}


def project_record_event(root: Path, name: str, summary: str, event_type: str = "work", related_files: list[str] | None = None) -> dict[str, Any]:
    return project_capture(
        root,
        name,
        summary,
        kind="event",
        sources=related_files or [],
        confidence="high",
        metadata={"event_type": event_type},
        refresh=True,
    )


def project_pause(
    root: Path,
    name: str = "",
    session_id: str | None = None,
    *,
    summary: str = "",
    details: str = "",
    sources: Iterable[Any] | None = None,
    uncertainty: Iterable[str] | None = None,
    likely_continuation: str = "",
    confidence: str = "medium",
) -> dict[str, Any]:
    project_id = clean_project_id(name) if name else current_project_id(root, session_id=session_id)
    if not project_id:
        return {"status": "not_attached", "reason": "No project is attached to pause."}
    captured = None
    if summary.strip():
        captured = project_capture(
            root,
            project_id,
            summary,
            kind="continuity_reflection",
            details=details,
            sources=sources,
            uncertainty=uncertainty,
            likely_continuation=likely_continuation,
            confidence=confidence,
            refresh=False,
        )

    # A natural-language "pause here" must not discard a final small activity
    # window merely because it did not cross the idle debounce threshold. Reuse
    # the same lock-safe observable checkpoint path as the OpenCode plugin.
    attached = current_session_project(root, session_id=session_id)
    if attached and attached.get("project_id") == project_id:
        import opencode_events

        checkpoint = opencode_events.checkpoint_session(
            root,
            str(attached.get("session_id") or session_id or SESSION_ID),
            reason="project.pause",
            detach=True,
            force=True,
        )
        paused = checkpoint.get("status") in {"checkpointed", "refreshed_without_capture"}
        return {
            "status": "paused" if paused else "pause_conflict",
            "project_id": project_id,
            "reflection": captured,
            "observable_checkpoint": checkpoint,
            "refresh": checkpoint.get("refresh") or refresh_project_files(root, project_id),
            "session": checkpoint.get("session"),
        }

    refresh = refresh_project_files(root, project_id)
    detached = detach_session_project(root, project_id, session_id=session_id)
    return {"status": "paused", "project_id": project_id, "reflection": captured, "refresh": refresh, "session": detached}
