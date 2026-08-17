from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import subprocess
import symtable
import time
import tokenize
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import indexing_policy
import project_workspace
import rag_backend
import safety

from . import provenance, store, vector_store
from .languages import detect_language, parser_runtime_profile
from .models import BranchIdentity, EvidenceLocator, SourceRevision
from .parser import parse_source

ENGINE_VERSION = "awoki-structural-code-v8"
MAX_FILE_BYTES = 2_000_000
CODE_IGNORE_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target",
    "dist", "build", ".mypy_cache", ".ruff_cache", ".tox", "coverage", ".next",
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.:-]*$")

CODE_TEXT_EXTENSIONS = {
    ".asm", ".s", ".yara", ".yar",
    ".vue", ".svelte", ".kt", ".kts", ".scala", ".lua", ".r", ".swift",
    ".dart", ".ex", ".exs", ".erl", ".hrl", ".sol", ".tf", ".hcl",
    ".yaml", ".yml", ".toml", ".json", ".xml", ".gradle", ".properties",
}
CODE_FALLBACK_SOURCE_EXTENSIONS = {
    # Known textual source / interface / policy languages without dedicated
    # structural parsers. This is a fast-path hint only; unknown textual
    # non-prose files also receive deterministic text-fallback chunks.
    ".proto", ".rego", ".graphql", ".gql", ".ps1", ".hs", ".lhs",
    ".clj", ".cljs", ".cljc", ".edn", ".fs", ".fsx", ".vb",
    ".groovy", ".nim", ".zig", ".v", ".d", ".cmake",
}
CODE_FILENAMES = {"Dockerfile", "Makefile", "Rakefile", "Gemfile", "Jenkinsfile"}
PROSE_TEXT_SUFFIXES = {".md", ".markdown", ".rst", ".adoc", ".txt", ".log", ".csv", ".tsv"}
PROSE_TEXT_NAMES = {"readme", "license", "notice", "changelog", "authors", "contributors"}


def _is_source_like(path: Path) -> bool:
    """Return whether a repository path is a plausible textual analysis input.

    Structural parser support is intentionally not the boundary for exhaustive
    lexical coverage. For existing files, fall back to a conservative content
    text check; for missing/symlink paths, known extensions/names remain useful
    for completeness accounting.
    """
    if detect_language(path) is not None or path.suffix.lower() in CODE_TEXT_EXTENSIONS or path.name in CODE_FILENAMES:
        return True
    if path.is_symlink():
        return True
    try:
        if path.is_file():
            with path.open("rb") as handle:
                sample = handle.read(65536)
            return indexing_policy.looks_textual_bytes(sample)
    except OSError:
        return False
    return False


def _is_structural_source_like(path: Path) -> bool:
    """Return whether text should participate in primary code discovery.

    Parser support is never the gate. Unknown textual repository formats are
    admitted to the deterministic text fallback unless they are recognizably
    prose/log/tabular documents. This keeps unusual languages and configuration
    discoverable through codebase_search without dumping ordinary README prose
    into the code graph. Exact code_text_search remains broader still.
    """
    if detect_language(path) is not None:
        return True
    suffix = path.suffix.lower()
    if suffix in CODE_TEXT_EXTENSIONS or suffix in CODE_FALLBACK_SOURCE_EXTENSIONS or path.name in CODE_FILENAMES:
        return True
    lower_name = path.name.lower()
    stem = path.stem.lower() if path.suffix else lower_name
    if suffix in PROSE_TEXT_SUFFIXES or stem in PROSE_TEXT_NAMES or lower_name in PROSE_TEXT_NAMES:
        return False
    # Any remaining textual repository file is a legitimate generic code/config
    # fallback candidate. _is_source_like performs the binary/text check.
    return _is_source_like(path)


def _nested_git_roots(repo_root: Path, *, max_depth: int = 4) -> list[str]:
    """Find accidental nested Git worktrees without recursively walking huge trees."""
    found: list[str] = []
    if not repo_root.exists():
        return found
    root_depth = len(repo_root.parts)
    ignored = CODE_IGNORE_PARTS | {"vendor", ".cache"}
    try:
        for current, dirs, files in os.walk(repo_root, topdown=True, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.parts) - root_depth
            dirs[:] = [d for d in dirs if d not in ignored and not (current_path / d).is_symlink()]
            if depth >= max_depth:
                dirs[:] = []
            marker_dir = current_path / ".git"
            if marker_dir.is_dir() or ".git" in files:
                try:
                    rel = current_path.relative_to(repo_root).as_posix() or "."
                except ValueError:
                    continue
                if rel != ".":
                    found.append(rel)
                    dirs[:] = []
                    if len(found) >= 8:
                        break
    except OSError:
        pass
    return sorted(set(found))


def _sanitize_parsed_source(parsed: Any) -> Any:
    """Redact actual credential literals from derived source text only.

    Parsing always uses the exact repository bytes so symbol positions and call
    relationships remain authoritative. Before chunks, signatures, reference
    source, and control-context text are persisted or embedded, high-confidence
    source secrets are redacted. Secret-like identifiers/expressions are left
    intact.
    """
    def clean(text: str) -> str:
        return safety.redact_source_text(text)[0]

    return replace(
        parsed,
        symbols=tuple(replace(symbol, signature=clean(symbol.signature)) for symbol in parsed.symbols),
        chunks=tuple(
            replace(chunk, title=clean(chunk.title), text=clean(chunk.text))
            for chunk in parsed.chunks
        ),
        references=tuple(
            replace(
                reference,
                source_text=clean(reference.source_text),
                control_context=tuple(clean(value) for value in reference.control_context),
            )
            for reference in parsed.references
        ),
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="ignore")
    return hashlib.sha256(value).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _run_git(repo_root: Path, *args: str) -> tuple[int, str]:
    try:
        env = provenance.sanitized_git_environment()
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), *args],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _resolve_repo_spec(paths: Any, project_id: str, repo: str = "", *, require_unique: bool = True) -> dict[str, Any]:
    return project_workspace.resolve_project_repository(paths.root, project_id, repo, require_unique=require_unique)


def _resolve_source_spec(
    paths: Any,
    project_id: str,
    source: str = "",
    *,
    repo: str = "",
    require_unique: bool = True,
) -> dict[str, Any]:
    if source and repo and source != repo:
        return {
            "status": "rejected",
            "project_id": project_id,
            "reason": "source= and repo= identify different analysis scopes; provide only one",
        }
    return project_workspace.resolve_project_source(
        paths.root, project_id, source, repo_id=repo, require_unique=require_unique
    )


def _repo_manifest_path(project_dir: Path, repo_name: str, *, legacy: bool) -> Path:
    if legacy:
        return store.manifest_path(project_dir)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo_name).strip(".-") or "repo"
    return project_dir / "index" / "manifests" / f"code-index-{safe}.json"


def _source_manifest_path(project_dir: Path, resolved: dict[str, Any]) -> Path:
    if str(resolved.get("source_type") or "git") == "git":
        return _repo_manifest_path(
            project_dir,
            str(resolved.get("repo_id") or resolved.get("source_id") or "default"),
            legacy=bool(resolved.get("legacy")),
        )
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(resolved.get("source_id") or "source")).strip(".-") or "source"
    return project_dir / "index" / "manifests" / f"code-index-source-{safe}.json"


def _filesystem_source_revision(
    project_id: str, source_root: Path, *, source_id: str, source_type: str
) -> SourceRevision:
    identity = project_workspace.source_manifest_identity(source_root)
    content_identity = str(identity.get("content_identity") or "")
    revision_key = f"source:{source_id}|sha256:{content_identity}" if content_identity else f"source:{source_id}|unavailable"
    label = f"{source_type}:{content_identity[:12] or 'unavailable'}"
    return SourceRevision(
        source_id=source_id,
        source_type=source_type,
        revision_key=revision_key,
        revision_label=label,
        content_identity=content_identity,
        dirty=False,
        provenance={
            "identity_source": "content_manifest",
            "manifest_file_count": int(identity.get("file_count") or 0),
        },
        repo_id="",
        branch_key=revision_key,
        branch_name=label,
        commit_sha="",
        source="content_manifest",
    )


def source_revision(project_id: str, resolved: dict[str, Any]) -> SourceRevision:
    root = Path(resolved["root"])
    source_id = str(resolved.get("source_id") or resolved.get("repo_id") or "default")
    source_type = str(resolved.get("source_type") or "git")
    if source_type == "git":
        branch = branch_identity(
            project_id, root,
            repo_name=str(resolved.get("repo_id") or source_id or "default"),
            legacy=bool(resolved.get("legacy")),
        )
        return SourceRevision.from_branch(branch, source_id=source_id)
    return _filesystem_source_revision(project_id, root, source_id=source_id, source_type=source_type)


def _source_evidence(root: Path, revision: SourceRevision, *, deep: bool) -> dict[str, Any]:
    if revision.source_type == "git":
        return provenance.collect_repository_evidence(root, deep=deep)
    current = project_workspace.source_manifest_identity(root)
    identity = str(current.get("content_identity") or "")
    return {
        "status": "content_manifest",
        "assurance": "CONTENT_MANIFEST_BOUND",
        "source_id": revision.source_id,
        "source_type": revision.source_type,
        "revision_key": revision.revision_key,
        "content_identity": identity,
        "view_fingerprint": identity,
        "file_count": int(current.get("file_count") or 0),
        "anomalies": [],
        "limitations": [
            "content identity binds the registered filesystem corpus bytes; no Git history or commit authenticity is claimed"
        ],
    }


def _published_vector_collection(manifest: dict[str, Any] | None) -> str:
    """Return the collection bound to the last successfully published vectors.

    R8 manifests predate the explicit field, so a successful/current legacy
    ``vector`` record is accepted as a backwards-compatible anchor. A stale or
    degraded target collection must never overwrite this identity.
    """
    if not isinstance(manifest, dict):
        return ""
    explicit = str(manifest.get("published_vector_collection") or "")
    if explicit:
        return explicit
    vector = manifest.get("vector") or {}
    if isinstance(vector, dict) and str(vector.get("status") or "") in {"indexed", "current"}:
        return str(vector.get("collection") or "")
    return ""


def branch_identity(project_id: str, repo_root: Path, *, repo_name: str = "default", legacy: bool = True) -> BranchIdentity:
    repo_id = f"{project_id}:repo" if legacy else f"{project_id}:{repo_name}"
    branch_prefix = "" if legacy else f"repo:{repo_name}|"
    top_rc, toplevel = _run_git(repo_root, "rev-parse", "--show-toplevel")
    if top_rc == 0 and toplevel:
        try:
            exact_root = bool(toplevel) and Path(toplevel).resolve() == repo_root.resolve()
        except OSError:
            exact_root = False
        if not exact_root:
            return BranchIdentity(
                repo_id=repo_id,
                branch_key=f"{branch_prefix}working-tree:git-root-mismatch",
                branch_name="(invalid repo root)",
                commit_sha="",
                dirty=True,
                source="git_root_mismatch",
            )
        identity = provenance.passive_git_identity(repo_root, untracked="normal")
        commit = str(identity.get("commit_sha") or "")
        branch = str(identity.get("branch_name") or "")
        status = "\n".join(str(item) for item in identity.get("entries") or [])
        if not identity.get("available", False):
            # Conservative fallback: inability to prove clean must not produce
            # a reusable clean-snapshot identity.
            commit_rc, commit_value = _run_git(repo_root, "rev-parse", "HEAD")
            branch_rc, branch_value = _run_git(repo_root, "branch", "--show-current")
            commit = commit_value if commit_rc == 0 else ""
            branch = branch_value if branch_rc == 0 else ""
            status = "status-unavailable"
        if branch:
            branch_key = f"{branch_prefix}branch:{branch}"
            source = "git_branch"
        elif commit:
            branch_key = f"{branch_prefix}detached:{commit}"
            branch = "(detached)"
            source = "git_detached"
        else:
            branch_key = f"{branch_prefix}working-tree:unknown"
            branch = "(unknown)"
            source = "git_unknown"
        return BranchIdentity(
            repo_id=repo_id,
            branch_key=branch_key,
            branch_name=branch,
            commit_sha=commit,
            dirty=bool(status),
            source=source,
        )
    nested = _nested_git_roots(repo_root)
    if nested:
        return BranchIdentity(
            repo_id=repo_id,
            branch_key=f"{branch_prefix}working-tree:nested-git-root-mismatch",
            branch_name="(invalid repo root)",
            commit_sha="",
            dirty=True,
            source="nested_git_root_mismatch",
        )
    return BranchIdentity(
        repo_id=repo_id,
        branch_key=f"{branch_prefix}working-tree:non-git",
        branch_name="(non-git)",
        commit_sha="",
        dirty=True,
        source="non_git",
    )


@contextmanager
def _index_lock(project_dir: Path):
    lock_path = project_dir / "index" / "code-index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _vector_lock(root: Path):
    """Serialize shared Qdrant membership read-modify-write operations."""
    lock_path = root / ".harness" / "state" / "code-vector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _candidate_files(repo_root: Path, *, include_ignored: bool = False) -> Iterable[Path]:
    if not repo_root.exists():
        return []
    # Normal indexing/search follows Git's ignore policy. Explicit forensic
    # lexical search can opt into ignored untracked files; structural indexing
    # never enables this flag implicitly.
    inside_rc, inside = _run_git(repo_root, "rev-parse", "--is-inside-work-tree")
    if inside_rc == 0 and inside == "true":
        try:
            commands = [
                ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), "ls-files", "-co", "--exclude-standard", "-z"],
            ]
            if include_ignored:
                commands.append([
                    "git", "-c", "core.fsmonitor=false", "-C", str(repo_root), "ls-files", "--others", "--ignored",
                    "--exclude-standard", "-z",
                ])
            git_paths: list[Path] = []
            for command in commands:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    env=provenance.sanitized_git_environment(),
                )
                if completed.returncode != 0:
                    raise subprocess.SubprocessError(f"git candidate enumeration failed: {completed.returncode}")
                for raw in completed.stdout.split(b"\0"):
                    if not raw:
                        continue
                    rel_text = os.fsdecode(raw)
                    rel = Path(rel_text)
                    if rel.is_absolute() or ".." in rel.parts:
                        continue
                    path = repo_root / rel
                    if not path.is_symlink() and not path.is_file():
                        continue
                    git_paths.append(path)
            return sorted(set(git_paths))
        except (OSError, subprocess.SubprocessError):
            pass
    out: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_symlink() and not path.is_file():
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        out.append(path)
    return sorted(out)


def _scan_repository(paths: Any, project_id: str, repo_root: Path, *, include_ignored: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    probe_rows: list[str] = []
    canonical_workspace_root = Path(paths.root).resolve()
    for path in _candidate_files(repo_root, include_ignored=include_ignored):
        try:
            rel_repo = path.relative_to(repo_root).as_posix()
            # Repository roots are canonicalized by the multi-repo registry.
            # On macOS, tempfile/workspace paths commonly enter through /var
            # while Path.resolve() canonicalizes them to /private/var. Compare
            # canonical paths on both sides or every source file is silently
            # skipped by relative_to(). Source symlinks are already excluded.
            rel_root = path.resolve().relative_to(canonical_workspace_root)
        except (OSError, ValueError):
            continue
        decision = indexing_policy.decide_file(
            path,
            rel=rel_root,
            category="code",
            redact=safety.redact_source_text,
            max_bytes=MAX_FILE_BYTES,
        )
        repo_candidate = bool(_is_source_like(path))
        lexical_included = bool(decision.included and repo_candidate)
        lexical_only = decision.reason in {
            "sensitive_text_lexical_only", "large_text_lexical_only", "generated_text_lexical_only"
        }
        structural_included = bool(decision.included and _is_structural_source_like(path) and not lexical_only)
        policy_reason = decision.reason
        row = {
            **decision.as_dict(),
            "repo_relative": rel_repo,
            "absolute_path": str(path),
            "repository_source_candidate": repo_candidate,
            "lexical_included": lexical_included,
            "policy_reason": policy_reason,
            "lexical_only": lexical_only,
            "sensitive_lexical_only": decision.reason == "sensitive_text_lexical_only",
            "structural_parser": (detect_language(path).name if detect_language(path) is not None else ("text_fallback" if _is_structural_source_like(path) else "none")),
        }
        if decision.included and not structural_included:
            row["included"] = False
            row["reason"] = "lexical_only_policy" if lexical_only else "unsupported_code_extension"
        probe_rows.append(
            f"{rel_repo}\0{int(bool(row.get('included')))}\0{int(lexical_included)}\0{row.get('reason')}\0{policy_reason}\0{decision.content_hash}\0{decision.size_bytes}"
        )
        (included if row.get("included") else excluded).append(row)
    return included, excluded, _sha("\n".join(sorted(probe_rows)))


def preview_project_code(
    paths: Any, project_id: str, *, repo: str = "", source: str = ""
) -> dict[str, Any]:
    """Preview the exact structural-code eligibility set for one evidence source."""
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, _db, resolved = context
    if not repo_root.exists():
        return {
            "status": "not_found",
            "project_id": project_id,
            "repo_id": resolved.get("repo_id"),
            "repo_root": str(repo_root),
            "included": [],
            "excluded": [],
            "reason": "source directory does not exist",
        }
    if branch.source_type == "git" and branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_id": resolved.get("repo_id"),
            "repo_root": str(repo_root),
            "reason": "configured repository is not the exact Git worktree root",
            "nested_git_roots": _nested_git_roots(repo_root),
            "fix": "register the exact Git worktree root, e.g. repo/oathkeeper",
        }
    included, excluded, source_probe_hash = _scan_repository(paths, project_id, repo_root)

    def portable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{key: value for key, value in row.items() if key != "absolute_path"} for row in rows]

    return {
        "status": "preview",
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "source_id": resolved.get("source_id"),
        "source_type": resolved.get("source_type"),
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "branch": branch.__dict__,
        "source_probe_hash": source_probe_hash,
        "included": portable(included),
        "excluded": portable(excluded),
    }


def _document_set_hash(rows: list[dict[str, Any]]) -> str:
    values = [
        f"{row.get('chunk_id')}|{row.get('embedding_key')}|{row.get('path')}|{row.get('start_line')}|{row.get('end_line')}"
        for row in rows
    ]
    return _sha("\n".join(sorted(values)))


def _write_manifest(project_dir: Path, payload: dict[str, Any]) -> None:
    indexing_policy.write_index_manifest(store.manifest_path(project_dir), payload)


def index_project_code(
    paths: Any,
    project_id: str,
    *,
    include_qdrant: bool = True,
    force: bool = False,
    repo: str = "",
    source: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, db, resolved = context
    if not repo_root.exists():
        return {
            "status": "not_found", "project_id": project_id,
            "repo_id": resolved.get("repo_id"), "source_id": resolved.get("source_id"),
            "reason": f"source directory does not exist: {repo_root}",
        }
    project_workspace.enable_code_index(paths.root, project_id)
    if branch.source_type == "git" and branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_root": str(repo_root),
            "repo_id": resolved.get("repo_id"),
            "reason": "configured repository is not the exact Git worktree root",
            "nested_git_roots": _nested_git_roots(repo_root),
            "fix": "register the exact Git worktree root, e.g. repo/oathkeeper",
        }
    initial_repository_evidence = _source_evidence(repo_root, branch, deep=True)
    repository_evidence = initial_repository_evidence
    parser_profile = parser_runtime_profile()
    parser_profile_hash = _json_hash(parser_profile)
    embedding_profile_hash = vector_store.embedding_profile_hash()
    included, excluded, source_probe_hash = _scan_repository(paths, project_id, repo_root)
    if progress_callback is not None:
        progress_callback({
            "phase": "source_scanned",
            "files_total": len(included),
            "files_processed": 0,
            "files_parsed": 0,
            "files_reused": 0,
            "files_removed": 0,
            "current_path": "",
            "parse_modes": {},
            "progress_percent": 0.0,
        })

    with _index_lock(pp.project_dir):
        # This snapshot represents what the last successful remote sync wrote,
        # not merely what the current SQLite source index contains. Keeping the
        # two separate makes deletion/retry correct after a Qdrant outage.
        synced_rows = store.synced_vector_memberships(db, project_id, branch.branch_key)
        prior = store.read_state(db, branch.branch_key)
        prior_manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
        prior_vector_collection = _published_vector_collection(prior_manifest)
        current_vector_collection = vector_store.code_collection_name()
        published_vector_collection = prior_vector_collection
        fresh_lexical = bool(
            prior
            and prior.get("source_probe_hash") == source_probe_hash
            and prior.get("parser_profile_hash") == parser_profile_hash
            and prior.get("embedding_profile_hash") == embedding_profile_hash
            and int(prior.get("schema_version") or 0) == store.SCHEMA_VERSION
            and prior.get("engine_version") == ENGINE_VERSION
        )
        changed_files: list[str] = []
        reused_files: list[str] = []
        parse_modes: dict[str, int] = {}
        reference_integrity = {
            "references_input": 0,
            "references_stored": 0,
            "duplicate_references_deduplicated": 0,
            "identity_conflicts": 0,
        }
        indexed_at = _now()
        current_paths: set[str] = set()

        if force or not fresh_lexical:
            progress_every = max(1, len(included) // 100)
            for entry_index, entry in enumerate(included, start=1):
                rel_path = str(entry["repo_relative"])
                current_paths.add(rel_path)
                previous = store.file_record(db, project_id, branch.branch_key, rel_path)
                if (
                    not force
                    and previous
                    and previous.get("content_hash") == entry.get("content_hash")
                    and prior.get("parser_profile_hash") == parser_profile_hash
                    and prior.get("embedding_profile_hash") == embedding_profile_hash
                ):
                    reused_files.append(rel_path)
                    if progress_callback is not None and (entry_index == 1 or entry_index == len(included) or entry_index % progress_every == 0):
                        progress_callback({
                            "phase": "structural_index",
                            "files_total": len(included),
                            "files_processed": entry_index,
                            "files_parsed": len(changed_files),
                            "files_reused": len(reused_files),
                            "files_removed": 0,
                            "current_path": rel_path,
                            "parse_modes": dict(parse_modes),
                            "progress_percent": round((entry_index / max(1, len(included))) * 90.0, 1),
                        })
                    continue
                source_path = Path(str(entry["absolute_path"]))
                if source_path.is_symlink() or not source_path.is_file():
                    return {
                        "status": "stale_source",
                        "project_id": project_id,
                        "reason": "repository source changed type during indexing; retry required",
                        "path": rel_path,
                    }
                data = source_path.read_bytes()
                observed_hash = indexing_policy.content_hash_bytes(data)
                expected_size_raw = entry.get("size_bytes")
                expected_size = int(expected_size_raw) if expected_size_raw is not None else -1
                if (
                    observed_hash != str(entry.get("content_hash") or "")
                    or len(data) != expected_size
                ):
                    # Never parse or embed bytes that differ from the exact bytes
                    # which passed the fail-closed eligibility scan. A retry
                    # performs policy checks against the new content.
                    return {
                        "status": "stale_source",
                        "project_id": project_id,
                        "reason": "repository source changed after eligibility validation; retry required",
                        "path": rel_path,
                        "expected_sha256": entry.get("content_hash"),
                        "observed_sha256": observed_hash,
                    }
                parsed = _sanitize_parsed_source(parse_source(rel_path, data, embedding_profile_hash))
                file_id = _sha(f"{project_id}|{branch.source_id}|{branch.revision_key}|{rel_path}|{entry['content_hash']}")
                try:
                    replace_stats = store.replace_file(
                        db,
                        file_id=file_id,
                        project_id=project_id,
                        branch=branch,
                        rel_path=rel_path,
                        content_hash=str(entry["content_hash"]),
                        size_bytes=int(entry["size_bytes"]),
                        parsed=parsed,
                        indexed_at=indexed_at,
                    )
                except store.ReferenceIdentityConflict as exc:
                    reference_integrity["identity_conflicts"] += 1
                    return {
                        "status": "rejected",
                        "project_id": project_id,
                        "reason": "parser reference identity conflict; structural index was not marked current",
                        "path": rel_path,
                        "error": str(exc),
                        "reference_integrity": reference_integrity,
                    }
                for key in (
                    "references_input",
                    "references_stored",
                    "duplicate_references_deduplicated",
                ):
                    reference_integrity[key] += int(replace_stats.get(key) or 0)
                changed_files.append(rel_path)
                parse_modes[parsed.parse_mode] = parse_modes.get(parsed.parse_mode, 0) + 1
                if progress_callback is not None and (entry_index == 1 or entry_index == len(included) or entry_index % progress_every == 0):
                    progress_callback({
                        "phase": "structural_index",
                        "files_total": len(included),
                        "files_processed": entry_index,
                        "files_parsed": len(changed_files),
                        "files_reused": len(reused_files),
                        "files_removed": 0,
                        "current_path": rel_path,
                        "parse_modes": dict(parse_modes),
                        "progress_percent": round((entry_index / max(1, len(included))) * 90.0, 1),
                    })
            removed_files = store.delete_stale_files(db, project_id, branch.branch_key, current_paths)
            if progress_callback is not None:
                progress_callback({
                    "phase": "resolving_call_graph",
                    "files_total": len(included),
                    "files_processed": len(included),
                    "files_parsed": len(changed_files),
                    "files_reused": len(reused_files),
                    "files_removed": len(removed_files),
                    "current_path": "",
                    "parse_modes": dict(parse_modes),
                    "progress_percent": 92.0,
                })
            graph = store.resolve_call_graph(db, branch.branch_key)
        else:
            current_paths = {str(entry["repo_relative"]) for entry in included}
            removed_files = []
            graph = {
                "resolved": 0,
                "ambiguous": 0,
                "unresolved": 0,
                "status": "unchanged",
            }

        if progress_callback is not None:
            progress_callback({
                "phase": "publishing_index",
                "files_total": len(included),
                "files_processed": len(included),
                "files_parsed": len(changed_files),
                "files_reused": len(reused_files),
                "files_removed": len(removed_files),
                "current_path": "",
                "parse_modes": dict(parse_modes),
                "progress_percent": 95.0,
            })
        metadata_updated_files = store.update_branch_metadata(
            db, project_id, branch, indexed_at
        )
        new_rows = store.branch_embedding_memberships(db, project_id, branch.branch_key)
        document_set_hash = _document_set_hash(new_rows)
        target_membership_hash = vector_store.membership_hash(new_rows, project_id, branch.branch_key)
        prior_vector_membership_hash = str(prior.get("qdrant_membership_hash") or "") if prior else ""
        published_vector_membership_hash = prior_vector_membership_hash
        prior_published_snapshot_matches_target = bool(
            prior
            and prior.get("qdrant_membership_hash") == target_membership_hash
            and prior.get("vector_status") == "indexed"
            and prior_vector_collection == current_vector_collection
        )
        preserve_prior_vector_snapshot = False
        vector_result: dict[str, Any]
        if include_qdrant:
            synced_membership_hash = vector_store.membership_hash(
                synced_rows, project_id, branch.branch_key
            )
            vector_is_current = bool(
                prior
                and prior.get("qdrant_membership_hash") == target_membership_hash
                and prior.get("vector_status") == "indexed"
                and prior_vector_collection == current_vector_collection
                and synced_membership_hash == target_membership_hash
                and not force
                and fresh_lexical
            )
            if vector_is_current:
                collection_ok, collection_reason = vector_store.collection_available()
                vector_is_current = collection_ok
            if vector_is_current:
                vector_result = {
                    "status": "current",
                    "membership_hash": target_membership_hash,
                    "collection": vector_store.code_collection_name(),
                    "new_vectors": 0,
                    "reused_vectors": 0,
                    "removed_memberships": 0,
                }
                published_vector_membership_hash = target_membership_hash
                published_vector_collection = current_vector_collection
            else:
                with _vector_lock(paths.root):
                    vector_result = vector_store.sync_branch_memberships(
                        project_id=project_id,
                        branch_key=branch.branch_key,
                        old_rows=synced_rows,
                        new_rows=new_rows,
                        progress_callback=progress_callback,
                    )
                if vector_result.get("status") in {"indexed", "current"}:
                    store.replace_synced_vector_memberships(
                        db, project_id, branch.branch_key, new_rows
                    )
                    published_vector_membership_hash = target_membership_hash
                    published_vector_collection = current_vector_collection
                elif prior_published_snapshot_matches_target:
                    # A failed redundant/forced refresh must not destroy a
                    # previously published snapshot that still represents the
                    # exact same semantic membership and collection. The job
                    # result remains degraded for truthful diagnostics, while
                    # search readiness keeps using the last known-good vectors.
                    preserve_prior_vector_snapshot = True
        else:
            if (
                prior.get("qdrant_membership_hash") == target_membership_hash
                and prior.get("vector_status") == "indexed"
                and prior_vector_collection == current_vector_collection
            ):
                vector_result = {
                    "status": "current",
                    "membership_hash": target_membership_hash,
                    "collection": vector_store.code_collection_name(),
                    "reason": "vector refresh not requested; prior membership is current",
                }
                published_vector_membership_hash = target_membership_hash
                published_vector_collection = current_vector_collection
            else:
                vector_result = {
                    "status": "stale",
                    "membership_hash": target_membership_hash,
                    "collection": vector_store.code_collection_name(),
                    "reason": (
                        "vector refresh not requested; configured code-vector collection changed"
                        if prior_vector_collection and prior_vector_collection != current_vector_collection
                        else "vector refresh not requested; lexical snapshot differs from the last successfully published vector membership"
                    ),
                }

        vector_status = (
            "indexed"
            if vector_result.get("status") in {"indexed", "current"} or preserve_prior_vector_snapshot
            else str(vector_result.get("status") or "degraded")
        )
        vector_reason = str(vector_result.get("reason") or "")
        if preserve_prior_vector_snapshot:
            vector_reason = (
                "last vector refresh failed, but the prior successfully published snapshot "
                "still matches the current semantic membership and collection; "
                f"refresh error: {vector_reason or vector_result.get('status') or 'unknown'}"
            )
            vector_result["published_snapshot_preserved"] = True
            vector_result["published_snapshot_status"] = "indexed"

        # A deep clean-snapshot proof captured before source enumeration must
        # not survive a repository/view mutation that occurs while indexing or
        # while a remote vector sync is in flight. Re-check the repository at
        # the commit point before publishing state/manifest. A changed Git
        # content-selection identity requires a retry; mutable index/stat-view
        # metadata is recorded separately and must not masquerade as corpus
        # staleness when HEAD/content selection is unchanged.
        final_repository_evidence = _source_evidence(repo_root, branch, deep=True)
        initial_view_fingerprint = str(initial_repository_evidence.get("view_fingerprint") or "")
        final_view_fingerprint = str(final_repository_evidence.get("view_fingerprint") or "")
        initial_content_view_fingerprint = provenance.content_view_fingerprint(initial_repository_evidence)
        final_content_view_fingerprint = provenance.content_view_fingerprint(final_repository_evidence)
        git_identity_changed = bool(
            branch.source_type == "git"
            and str(branch.source).startswith("git_")
            and (
                str(final_repository_evidence.get("head_sha") or "") != str(branch.commit_sha or "")
                or initial_content_view_fingerprint != final_content_view_fingerprint
            )
        )
        source_identity_changed = bool(
            branch.source_type != "git"
            and str(final_repository_evidence.get("content_identity") or "") != str(branch.content_identity or "")
        )
        verified_snapshot_lost = bool(
            branch.source_type == "git"
            and str(initial_repository_evidence.get("assurance") or "") == "VERIFIED_SNAPSHOT"
            and str(final_repository_evidence.get("assurance") or "") != "VERIFIED_SNAPSHOT"
        )
        if git_identity_changed or source_identity_changed or verified_snapshot_lost:
            return {
                "status": "stale_source",
                "project_id": project_id,
                "reason": "source snapshot/view changed during indexing; retry required",
                "initial_repository_assurance": initial_repository_evidence.get("assurance"),
                "final_repository_assurance": final_repository_evidence.get("assurance"),
                "initial_view_fingerprint": initial_view_fingerprint,
                "final_view_fingerprint": final_view_fingerprint,
                "initial_content_view_fingerprint": initial_content_view_fingerprint,
                "final_content_view_fingerprint": final_content_view_fingerprint,
                "initial_commit_sha": branch.commit_sha,
                "final_commit_sha": final_repository_evidence.get("head_sha", ""),
            }
        repository_evidence = final_repository_evidence

        store.write_state(
            db,
            branch=branch,
            project_id=project_id,
            source_probe_hash=source_probe_hash,
            parser_profile_hash=parser_profile_hash,
            embedding_profile_hash=embedding_profile_hash,
            document_set_hash=document_set_hash,
            qdrant_membership_hash=published_vector_membership_hash,
            indexed_at=indexed_at,
            vector_status=vector_status,
            vector_reason=vector_reason,
            engine_version=ENGINE_VERSION,
        )
        counts = store.counts(db, branch.branch_key)
        manifest = {
            "schema_version": store.SCHEMA_VERSION,
            "engine": "awoki-structural",
            "engine_version": ENGINE_VERSION,
            "index_policy_version": indexing_policy.INDEX_POLICY_VERSION,
            "project_id": project_id,
            "repo_id": branch.repo_id,
            "source_id": branch.source_id,
            "source_type": branch.source_type,
            "revision_key": branch.revision_key,
            "revision_label": branch.revision_label,
            "content_identity": branch.content_identity,
            "repo_root": str(repo_root),
            "source_root": str(repo_root),
            "branch_key": branch.branch_key,
            "branch_name": branch.branch_name,
            "branch_identity_source": branch.source,
            "commit_sha": branch.commit_sha,
            "dirty": branch.dirty,
            "repository_evidence": repository_evidence,
            "repository_view_fingerprint": str(repository_evidence.get("view_fingerprint") or ""),
            "repository_content_view_fingerprint": provenance.content_view_fingerprint(repository_evidence),
            "source_probe_hash": source_probe_hash,
            "parser_profile": parser_profile,
            "parser_profile_hash": parser_profile_hash,
            "embedding_profile": rag_backend.embedding_profile(),
            "embedding_profile_hash": embedding_profile_hash,
            "document_set_hash": document_set_hash,
            "qdrant_membership_hash": published_vector_membership_hash,
            "target_qdrant_membership_hash": target_membership_hash,
            "published_vector_collection": published_vector_collection,
            "counts": counts,
            "included": [
                {k: value for k, value in entry.items() if k != "absolute_path"}
                for entry in included
            ],
            "excluded": [
                {k: value for k, value in entry.items() if k != "absolute_path"}
                for entry in excluded
            ],
            "vector": vector_result,
            "graph": graph,
            "reference_integrity": reference_integrity,
            "indexed_at": indexed_at,
        }
        indexing_policy.write_index_manifest(_source_manifest_path(pp.project_dir, resolved), manifest)
        if progress_callback is not None:
            progress_callback({
                "phase": "structural_complete" if not include_qdrant else "index_complete",
                "files_total": len(included),
                "files_processed": len(included),
                "files_parsed": len(changed_files),
                "files_reused": len(reused_files),
                "files_removed": len(removed_files),
                "current_path": "",
                "parse_modes": dict(parse_modes),
                "progress_percent": 100.0,
            })
        return {
            "status": "indexed" if (changed_files or removed_files or force) else "current",
            "project_id": project_id,
            "repo_id": resolved.get("repo_id"),
            "source_id": resolved.get("source_id"),
            "source_type": resolved.get("source_type"),
            "source_root": str(repo_root),
            "repo_root": str(repo_root) if str(resolved.get("source_type") or "git") == "git" else "",
            "branch": {
                "key": branch.branch_key,
                "name": branch.branch_name,
                "commit_sha": branch.commit_sha,
                "dirty": branch.dirty,
                "source": branch.source,
                "source_id": branch.source_id,
                "source_type": branch.source_type,
                "revision_key": branch.revision_key,
                "content_identity": branch.content_identity,
                "assurance": repository_evidence.get("assurance", "FILESYSTEM_BOUND"),
                "tree_sha": repository_evidence.get("raw_tree_sha", ""),
            },
            "repository_evidence": repository_evidence,
            "database": str(db),
            "changed_files": changed_files,
            "reused_files": reused_files,
            "removed_files": removed_files,
            "metadata_updated_files": metadata_updated_files,
            "parse_modes": parse_modes,
            "counts": counts,
            "graph": graph,
            "reference_integrity": reference_integrity,
            "vector": vector_result,
            "manifest": str(_source_manifest_path(pp.project_dir, resolved)),
            "source_probe_hash": source_probe_hash,
            "document_set_hash": document_set_hash,
            "excluded_count": len(excluded),
        }


def _project_context(
    paths: Any, project_id: str, repo: str = "", source: str = ""
) -> tuple[Any, Path, SourceRevision, Path, dict[str, Any]]:
    pp = project_workspace.paths_for(paths.root, project_id)
    resolved = _resolve_source_spec(paths, project_id, source, repo=repo, require_unique=True)
    if resolved.get("status") != "ok":
        raise ValueError(json.dumps({k: v for k, v in resolved.items() if k != "root"}, sort_keys=True))
    source_root = Path(resolved["root"])
    revision = source_revision(project_id, resolved)
    return pp, source_root, revision, store.db_path(pp.project_dir), resolved


def _project_context_result(
    paths: Any, project_id: str, repo: str = "", source: str = ""
) -> tuple[tuple[Any, Path, SourceRevision, Path, dict[str, Any]] | None, dict[str, Any] | None]:
    pp = project_workspace.paths_for(paths.root, project_id)
    if not pp.project_json.exists():
        return None, {"status": "not_found", "project_id": project_id}
    resolved = _resolve_source_spec(paths, project_id, source, repo=repo, require_unique=True)
    if resolved.get("status") != "ok":
        return None, {k: v for k, v in resolved.items() if k != "root"}
    source_root = Path(resolved["root"])
    revision = source_revision(project_id, resolved)
    return (pp, source_root, revision, store.db_path(pp.project_dir), resolved), None


def ensure_current(
    paths: Any,
    project_id: str,
    *,
    include_qdrant: bool,
    force: bool = False,
    repo: str = "",
    source: str = "",
) -> dict[str, Any]:
    # A worktree can change between the eligibility scan and the exact byte
    # read. Retry once from a fresh policy scan; repeated churn is surfaced
    # instead of indexing unvalidated bytes or claiming freshness.
    result = index_project_code(paths, project_id, include_qdrant=include_qdrant, force=force, repo=repo, source=source)
    if result.get("status") == "stale_source":
        result = index_project_code(paths, project_id, include_qdrant=include_qdrant, force=force, repo=repo, source=source)
    return result


def _search_index_readiness(paths: Any, project_id: str, repo: str = "", source: str = "") -> dict[str, Any]:
    """Cheap readiness check for interactive structural search.

    A normal search must not turn into a repository-wide policy/hash scan or a
    remote vector rebuild. For a clean Git worktree whose previously materialized
    evidence says passive reuse is safe, commit identity plus the persisted
    Git-view fingerprint and parser/schema/engine state is sufficient to reuse
    the lexical structural snapshot. Reduced provenance such as a stable sparse
    view can still be reusable; status-suppressing index flags, weakened Git stat
    trust, and dirty worktrees remain conservative and require local revalidation. Vector readiness is tracked
    separately because changing the embedding profile must not invalidate
    otherwise-correct lexical/structural data.
    """
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, _db0, resolved = context
    if not repo_root.exists():
        return {
            "status": "not_found", "project_id": project_id, "repo_id": resolved.get("repo_id"),
            "source_id": resolved.get("source_id"), "reason": "source directory does not exist",
        }
    if branch.source_type == "git" and branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_root": str(repo_root),
            "branch": branch.__dict__,
            "reason": "configured project repo/ is not the exact Git worktree root",
            "nested_git_roots": _nested_git_roots(repo_root),
        }
    db = store.db_path(pp.project_dir)
    state = store.read_state(db, branch.branch_key) if db.exists() else {}
    if not state:
        return {
            "status": "not_indexed",
            "project_id": project_id,
            "source_id": resolved.get("source_id"),
            "source_type": branch.source_type,
            "source_root": str(repo_root),
            "repo_root": str(repo_root) if branch.source_type == "git" else "",
            "branch": branch.__dict__,
            "lexical_current": False,
            "vector_current": False,
            "reason": "active-branch structural index is absent",
        }

    parser_hash = _json_hash(parser_runtime_profile())
    embedding_hash = vector_store.embedding_profile_hash()
    manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
    indexed_vector_collection = _published_vector_collection(manifest)
    current_vector_collection = vector_store.code_collection_name()
    indexed_view_fingerprint = str((manifest or {}).get("repository_view_fingerprint") or "")
    indexed_repository_evidence = (manifest or {}).get("repository_evidence") or {}
    view_drift: dict[str, Any] = {}
    if branch.source_type == "git":
        indexed_passive_reuse_safe = provenance.passive_index_reuse_safe(indexed_repository_evidence)
        current_view = provenance.light_view_state(
            repo_root,
            known_head=branch.commit_sha,
            exact_root_verified=branch.source in {"git_branch", "git_detached", "git_unknown"},
        )
        current_view_fingerprint = str(current_view.get("view_fingerprint") or "")
        indexed_content_view_fingerprint = str(
            (manifest or {}).get("repository_content_view_fingerprint")
            or provenance.content_view_fingerprint(indexed_repository_evidence)
        )
        current_content_view_fingerprint = provenance.content_view_fingerprint(current_view)
        repository_view_current = bool(indexed_view_fingerprint) and indexed_view_fingerprint == current_view_fingerprint
        content_view_current = bool(indexed_content_view_fingerprint) and indexed_content_view_fingerprint == current_content_view_fingerprint

        assurance_reasons: list[str] = []
        if bool(current_view.get("ignore_stat")):
            assurance_reasons.append("git_ignore_stat_active")
        if not bool(current_view.get("trust_ctime", True)):
            assurance_reasons.append("git_ctime_trust_disabled")
        if str(current_view.get("check_stat") or "default").lower() == "minimal":
            assurance_reasons.append("git_checkstat_minimal")

        flag_probe: dict[str, Any] = {"status": "not_needed", "available": True}
        if not repository_view_current:
            raw_flags = provenance.passive_index_flag_state(repo_root)
            flag_probe = {
                "status": "checked" if raw_flags.get("available") else "unavailable",
                "available": bool(raw_flags.get("available")),
                "assume_unchanged_count": int(raw_flags.get("assume_unchanged_count") or 0),
                "skip_worktree_count": int(raw_flags.get("skip_worktree_count") or 0),
            }
            if not raw_flags.get("available"):
                assurance_reasons.append("index_flags_unavailable")
            if int(raw_flags.get("assume_unchanged_count") or 0):
                assurance_reasons.append("assume_unchanged_index_entries")
            if int(raw_flags.get("skip_worktree_count") or 0) and not bool(current_view.get("sparse_checkout")):
                assurance_reasons.append("manual_skip_worktree_index_entries")

        passive_assurance_ok = not assurance_reasons
        snapshot_reusable = bool(
            not branch.dirty
            and not bool(state.get("dirty"))
            and str(state.get("commit_sha") or "") == branch.commit_sha
            and indexed_passive_reuse_safe
            and content_view_current
            and passive_assurance_ok
        )
        identity_check_name = "git_content_snapshot"
        view_drift = {
            "content_view_current": content_view_current,
            "repository_view_current": repository_view_current,
            "metadata_view_drift": bool(content_view_current and not repository_view_current),
            "indexed_content_view_fingerprint": indexed_content_view_fingerprint,
            "current_content_view_fingerprint": current_content_view_fingerprint,
            "indexed_repository_view_fingerprint": indexed_view_fingerprint,
            "current_repository_view_fingerprint": current_view_fingerprint,
            "assurance_probe": flag_probe,
            "assurance_reasons": sorted(set(assurance_reasons)),
        }
    else:
        indexed_passive_reuse_safe = True
        current_view_fingerprint = branch.content_identity
        snapshot_reusable = bool(
            branch.content_identity
            and str(state.get("source_id") or "") == branch.source_id
            and str(state.get("revision_key") or "") == branch.revision_key
            and str(state.get("content_identity") or "") == branch.content_identity
            and indexed_view_fingerprint == branch.content_identity
        )
        identity_check_name = "content_manifest_identity"
    lexical_checks = {
        identity_check_name: snapshot_reusable,
        "indexed_passive_reuse_safe": indexed_passive_reuse_safe,
        "parser_profile": state.get("parser_profile_hash") == parser_hash,
        "schema": int(state.get("schema_version") or 0) == store.SCHEMA_VERSION,
        "engine": state.get("engine_version") == ENGINE_VERSION,
    }
    lexical_current = all(lexical_checks.values())
    vector_checks = {
        "lexical_current": lexical_current,
        "embedding_profile": state.get("embedding_profile_hash") == embedding_hash,
        "vector_collection": bool(indexed_vector_collection) and indexed_vector_collection == current_vector_collection,
        "vector_status": state.get("vector_status") == "indexed",
        "membership_present": bool(state.get("qdrant_membership_hash")),
    }
    vector_current = all(vector_checks.values())
    return {
        "status": "current" if lexical_current else "stale",
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "source_id": resolved.get("source_id"),
        "source_type": branch.source_type,
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "branch": branch.__dict__,
        "lexical_current": lexical_current,
        "vector_current": vector_current,
        "lexical_checks": lexical_checks,
        "vector_checks": vector_checks,
        "view_drift": view_drift,
        "indexed_vector_collection": indexed_vector_collection,
        "current_vector_collection": current_vector_collection,
        "state": state,
        "indexed_repository_evidence": indexed_repository_evidence,
        "repository_assurance": (
            str(indexed_repository_evidence.get("assurance") or ("WORKING_TREE_BOUND" if branch.source_type == "git" else "CONTENT_MANIFEST_BOUND"))
            if snapshot_reusable else ("WORKING_TREE_BOUND" if branch.source_type == "git" else "CONTENT_MANIFEST_BOUND")
        ),
        "reason": (
            "indexed Git content snapshot reusable; mutable repository-view metadata drift is non-stale"
            if branch.source_type == "git" and lexical_current and bool(view_drift.get("metadata_view_drift"))
            else "indexed Git content snapshot reusable" if branch.source_type == "git" and lexical_current
            else "indexed content-manifest source revision reusable" if branch.source_type != "git" and lexical_current
            else "structural snapshot requires refresh before authoritative search"
        ),
    }


def route_query(query: str, explicit_mode: str = "auto") -> dict[str, str]:
    mode = (explicit_mode or "auto").strip().lower().replace("-", "_")
    allowed = {"auto", "lexical", "conceptual", "exact", "definition", "callers", "callees", "path", "similar"}
    if mode not in allowed:
        return {"mode": "invalid", "reason": f"unknown explicit mode {mode!r}"}
    if mode != "auto":
        return {"mode": mode, "reason": "explicit mode override"}
    value = " ".join(query.strip().split())
    lowered = value.lower()
    if (
        "->" in value
        or re.search(r"\b(trace|path|flow)\b.+\b(to|into|reach)\b", lowered)
        or re.search(r"\bcan\s+.+?\s+reach\s+.+", lowered)
    ):
        return {"mode": "path", "reason": "query asks for a path or execution flow"}
    if (
        re.search(r"\bwhat does\b.+\bcall\b", lowered)
        or re.search(r"\bwhich\s+(?:functions?|methods?|symbols?)\s+does\b.+\bcall\b", lowered)
        or lowered.startswith("callees of ")
    ):
        return {"mode": "callees", "reason": "query asks for callees"}
    if re.search(r"\b(who|what|which).{0,20}\bcalls?\b", lowered) or lowered.startswith("callers of "):
        return {"mode": "callers", "reason": "query asks for callers"}
    if re.search(r"\b(where|find|show|locate)\b.{0,40}\b(defined|definition|implemented|declared)\b", lowered):
        return {"mode": "definition", "reason": "query asks for a definition or implementation"}
    if re.search(r"\b(similar|analogous|duplicate implementation)\b", lowered):
        return {"mode": "similar", "reason": "query asks for similar code"}
    quoted = re.findall(r"[`\"]([^`\"]+)[`\"]", value)
    if quoted or (IDENTIFIER_RE.match(value) and " " not in value):
        return {"mode": "exact", "reason": "query is an exact identifier or quoted literal"}
    if re.search(r"\b(all uses|all references|every use|literal|regex)\b", lowered):
        return {"mode": "exact", "reason": "query asks for exact occurrences"}
    return {"mode": "conceptual", "reason": "natural-language conceptual repository question"}


def _extract_symbol_candidate(query: str) -> str:
    quoted = re.findall(r"[`\"]([^`\"]+)[`\"]", query)
    if quoted:
        return quoted[0].strip()
    # Smali descriptors are exact structural identities but intentionally do
    # not fit the ordinary source-language identifier grammar. Preserve the
    # complete class->method(descriptor)return token when it appears inside a
    # natural-language definition/caller/callee query.
    smali = re.search(r"(L[^\s]+;->[^\s(]+\([^)]*\)[^\s?.,]+)", query)
    if smali:
        return smali.group(1).strip()
    tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$.:-]*", query)
    stop = {
        "where", "find", "show", "locate", "defined", "definition", "implemented", "declared",
        "who", "what", "which", "calls", "call", "callers", "callees", "of", "does", "is", "are",
        "trace", "path", "flow", "from", "to", "into", "reach", "can", "the", "a", "an",
        "similar", "analogous", "duplicate", "implementation", "code",
    }
    candidates = [token for token in tokens if token.lower() not in stop]
    identifiers = [token for token in candidates if "_" in token or "." in token or any(char.isupper() for char in token)]
    return (identifiers or candidates or [query.strip()])[0]


def _path_endpoints(query: str) -> tuple[str, str] | None:
    if "->" in query:
        left, right = query.split("->", 1)
        return _extract_symbol_candidate(left), _extract_symbol_candidate(right)
    patterns = (
        r"(?:trace|path|flow)\s+(?:from\s+)?(.+?)\s+(?:to|into)\s+(.+)$",
        r"can\s+(.+?)\s+reach\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return _extract_symbol_candidate(match.group(1)), _extract_symbol_candidate(match.group(2))
    return None


def _hydrate_vector_rows(db: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        full = store.chunk_by_id(db, chunk_id) if chunk_id else None
        if full:
            full["score"] = float(row.get("score") or 0.0)
            full["retrieval_backend"] = "code_qdrant"
            out.append(full)
    return out


def _merge_ranked(query: str, hit_lists: Iterable[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    weights = {
        "definition_index": 2.0,
        "exact_structural": 1.7,
        "code_fts": 1.3,
        "code_qdrant": 1.0,
        "call_graph_callers": 1.8,
        "call_graph_callees": 1.8,
    }
    merged: dict[str, dict[str, Any]] = {}
    k = max(10, min(int(os.environ.get("AWOKI_CODE_RRF_K", "40")), 200))
    identifiers = set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", query))
    stage_names = {
        "code_fts": "fts",
        "code_qdrant": "qdrant",
        "exact_structural": "exact",
        "definition_index": "definition",
        "call_graph_callers": "callers",
        "call_graph_callees": "callees",
        "structural_promotion": "promotion",
    }
    for hits in hit_lists:
        for rank, hit in enumerate(hits, start=1):
            key = str(hit.get("chunk_id") or hit.get("symbol_id") or f"{hit.get('path')}:{hit.get('start_line')}:{hit.get('qualified_name')}")
            backend = str(hit.get("retrieval_backend") or "unknown")
            score = weights.get(backend, 0.6) / (k + rank)
            symbol = str(hit.get("symbol_name") or hit.get("name") or "")
            qualified = str(hit.get("qualified_name") or "")
            if symbol in identifiers or qualified in identifiers:
                score += 0.08
            existing = merged.get(key)
            if existing is None:
                item = dict(hit)
                item["rrf_score"] = score
                item["score"] = score
                item["retrieval_backends"] = [backend]
                item["raw_scores"] = {backend: float(hit.get("score") or 0.0)}
                stage = stage_names.get(backend, backend)
                item[f"{stage}_rank"] = rank
                item[f"{stage}_raw_score"] = float(hit.get("score") or 0.0)
                merged[key] = item
            else:
                existing["rrf_score"] = float(existing.get("rrf_score") or 0.0) + score
                existing["score"] = existing["rrf_score"]
                if backend not in existing["retrieval_backends"]:
                    existing["retrieval_backends"].append(backend)
                existing["raw_scores"][backend] = float(hit.get("score") or 0.0)
                stage = stage_names.get(backend, backend)
                existing[f"{stage}_rank"] = rank
                existing[f"{stage}_raw_score"] = float(hit.get("score") or 0.0)
    out = sorted(merged.values(), key=lambda row: float(row.get("rrf_score") or 0.0), reverse=True)
    for rank, row in enumerate(out, start=1):
        row["fused_rank"] = rank
        row["fused_score"] = float(row.get("rrf_score") or 0.0)
    return out[: max(1, min(limit, 200))]


def _result_focus(query: str, explicit: str = "auto") -> dict[str, str]:
    requested = (explicit or "auto").strip().lower().replace("-", "_")
    allowed = {"auto", "implementation", "balanced", "tests", "config"}
    if requested not in allowed:
        return {"focus": "invalid", "reason": f"unknown result_focus {requested!r}"}
    if requested != "auto":
        return {"focus": requested, "reason": "explicit result focus"}
    value = query.lower()
    test_terms = bool(re.search(r"\b(test|tests|testing|spec|specs|fixture|fixtures|regression|coverage)\b", value))
    test_negated = bool(re.search(
        r"\b(ignore|exclude|excluding|without|omit|omitting|not|rather than)\b[^.!?]{0,48}\b(test|tests|fixtures?|specs?)\b",
        value,
    ))
    production_explicit = bool(re.search(r"\b(production|implementation|runtime code|source implementation)\b", value))
    if test_terms and not test_negated and not production_explicit:
        return {"focus": "tests", "reason": "query explicitly asks for test evidence"}
    if re.search(r"\b(config|configuration|schema|setting|settings|option|options)\b", value) and not re.search(
        r"\b(implemented|implementation|handled|handles|enforced|enforces|processed|processes|where|code|function|method)\b",
        value,
    ):
        return {"focus": "config", "reason": "query primarily asks for configuration"}
    if re.search(
        r"\b(where|find|locate|show|how|implementation|implemented|handles?|handled|enforces?|enforced|"
        r"rejects?|rejected|allows?|allowed|decides?|processed|processing|before|after|upstream|authorization|authentication)\b",
        value,
    ):
        return {"focus": "implementation", "reason": "behavior/implementation-oriented repository question"}
    return {"focus": "balanced", "reason": "no strong authority-role intent detected"}


def _authority_class(row: dict[str, Any]) -> str:
    role = indexing_policy.source_role(str(row.get("path") or ""))
    kind = str(row.get("symbol_kind") or row.get("kind") or "").strip().lower()
    text_prefix = str(row.get("text") or "")[:600].lower()
    if "code generated" in text_prefix and "do not edit" in text_prefix:
        return "generated_or_stub"
    if role == "test":
        return "test"
    if role == "test_fixture":
        return "test_fixture"
    if role == "config_schema":
        return "config_schema"
    if role == "documentation":
        return "documentation"
    if role == "generated_or_vendor":
        return "generated_or_stub"
    if role == "production" and kind in {"function", "method", "constructor", "function_definition", "method_definition"}:
        return "production_implementation"
    if role == "production" and kind in {"interface", "trait"}:
        return "production_contract"
    if role == "production" and kind in {"module", "file", "namespace"}:
        # A coarse production container can be an excellent discovery hit, but
        # it is not itself a concrete implementation. R9.1 refines these rows
        # into contained callable symbols before final implementation ranking.
        return "production_module"
    if role == "production":
        return "production_helper"
    return "unknown"


def _candidate_level(row: dict[str, Any]) -> str:
    kind = str(row.get("symbol_kind") or row.get("kind") or "").strip().lower()
    if kind in _CONCRETE_SYMBOL_KINDS:
        return "concrete_symbol"
    if kind in {"interface", "trait"}:
        return "contract"
    if kind in {"module", "namespace"}:
        return "module"
    if kind == "file":
        return "file"
    if kind in {"class", "struct", "record", "type", "enum"}:
        return "type"
    return "unknown"


def _query_signal_terms(query: str, *, extra_stop: set[str] | None = None) -> set[str]:
    """Return bounded, non-generic terms useful for local relevance checks.

    This intentionally mirrors the spirit of the FTS stop-word filter without
    depending on FTS internals. It is used only as a conservative safeguard for
    structurally promoted candidates when a remote reranker is absent/fails.
    """
    stop = {
        "a", "an", "and", "are", "as", "at", "be", "been", "before", "by", "can",
        "code", "does", "find", "for", "from", "how", "in", "into", "is", "it", "its",
        "of", "on", "or", "show", "that", "the", "their", "then", "this", "to", "where",
        "which", "with", "would", "could", "should", "when", "what", "who", "why",
    }
    stop.update(value.casefold() for value in (extra_stop or set()))
    out: set[str] = set()
    # R9.1.5 made lexical discovery syntax-neutral across separator and camel
    # conventions. Reuse the same canonical identifier atoms for local
    # relevance checks so focus/refill policy does not regress to a different,
    # language-specific tokenizer at the next pipeline stage.
    raw_terms = re.findall(r"[A-Za-z_$][A-Za-z0-9_$.:/\-]{1,}", str(query or ""))
    for raw in raw_terms:
        aliases = store.identifier_lexemes(raw) or [raw.casefold()]
        for term in aliases:
            normalized = term.casefold()
            if len(normalized) < 3 or normalized in stop:
                continue
            out.add(normalized)
            if len(out) >= 24:
                return out
    return out


def _local_query_overlap(
    query: str,
    row: dict[str, Any],
    *,
    extra_stop: set[str] | None = None,
) -> float:
    """Cheap independent relevance signal for promoted graph candidates."""
    terms = _query_signal_terms(query, extra_stop=extra_stop)
    if not terms:
        return 0.0
    haystack = " ".join(
        str(row.get(field) or "")
        for field in ("symbol_name", "qualified_name", "signature", "path", "text")
    ).lower()
    matched = sum(1 for term in terms if term in haystack)
    return matched / max(1, len(terms))


_FOCUS_ROLE_QUERY_STOP = {
    "tests": {"test", "tests", "testing", "verify", "verifies", "verified", "demonstrate", "demonstrates"},
    "config": {"config", "configuration", "schema", "schemas", "setting", "settings"},
}


def _annotate_authority(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["source_role"] = indexing_policy.source_role(str(item.get("path") or ""))
        item["authority_class"] = _authority_class(item)
        out.append(item)
    return out


def _apply_authority_prior(query: str, rows: list[dict[str, Any]], focus: str) -> list[dict[str, Any]]:
    """Apply a scale-safe authority prior after relevance scoring.

    Authority is deliberately multiplicative and confidence-gated. Retrieval
    relevance remains the dominant signal across arbitrary scorer scales; an
    unrelated production function must never leap over a substantially stronger
    semantic/schema/test hit just because it is production code.
    """
    if not rows:
        return []

    if focus == "implementation":
        pct = {
            "production_implementation": 0.18,
            "production_helper": 0.07,
            "production_module": 0.00,
            "production_contract": -0.02,
            "test": -0.04,
            "test_fixture": -0.10,
            "config_schema": -0.12,
            "generated_or_stub": -0.11,
            "documentation": -0.10,
        }
    elif focus == "tests":
        pct = {
            "test": 0.18,
            "test_fixture": 0.12,
            "production_implementation": 0.0,
            "production_helper": -0.02,
            "production_module": -0.02,
            "production_contract": -0.01,
            "config_schema": -0.06,
            "generated_or_stub": -0.07,
            "documentation": -0.04,
        }
    elif focus == "config":
        pct = {
            "config_schema": 0.16,
            "production_implementation": 0.05,
            "production_helper": 0.03,
            "production_module": 0.01,
            "production_contract": 0.0,
            "test": -0.05,
            "test_fixture": -0.06,
            "generated_or_stub": -0.05,
            "documentation": 0.0,
        }
    else:
        pct = {
            "production_implementation": 0.05,
            "production_helper": 0.02,
            "production_module": 0.0,
            "production_contract": 0.0,
            "test": 0.0,
            "test_fixture": -0.015,
            "config_schema": -0.025,
            "generated_or_stub": -0.035,
            "documentation": -0.025,
        }

    ranked: list[dict[str, Any]] = []
    for row in _annotate_authority(rows):
        item = dict(row)
        score = float(item.get("score") or 0.0)
        authority = str(item.get("authority_class") or "unknown")
        backends = set(str(value) for value in (item.get("retrieval_backends") or []) if value)
        if item.get("retrieval_backend"):
            backends.add(str(item.get("retrieval_backend")))
        dual_retrieval = {"code_fts", "code_qdrant"}.issubset(backends)
        overlap = _local_query_overlap(query, item)
        rerank_rank = int(item.get("rerank_rank") or 0)
        rerank_signal = 0.0
        if item.get("rerank_score") is not None and rerank_rank > 0:
            # A bounded top reranker position is independent query-relevance
            # evidence. Keep it rank-based so different reranker score scales
            # cannot distort the authority gate.
            rerank_signal = max(0.0, 1.0 - (rerank_rank - 1) / 10.0)
        # Independent support is exposed for the later diversity/representation
        # stage. Dual lexical+semantic agreement is deliberately strong; local
        # query overlap is only a bounded fallback and can never prove behavior.
        # Reranker rank is also only relevance evidence, never behavioral proof.
        relevance_signal = max(
            1.0 if dual_retrieval else 0.0,
            min(1.0, overlap * 3.0),
            rerank_signal,
        )
        item["authority_relevance_signal"] = relevance_signal
        item["authority_dual_backend_support"] = dual_retrieval
        item["authority_query_overlap"] = overlap
        item["authority_rerank_signal"] = rerank_signal

        percentage = float(pct.get(authority, 0.0))
        if percentage > 0 and authority.startswith("production_"):
            # Keep a tiny scale-safe tie-breaking preference for production, but
            # reserve the full authority boost for candidates with independent
            # lexical+semantic agreement or local query support.
            confidence = 0.20 + 0.80 * relevance_signal
            if (item.get("promotion_candidate_only") or item.get("refinement_candidate_only")) and item.get("rerank_score") is None:
                # Structural reachability proves connectivity only. A promoted
                # or refined candidate without an independent reranker score
                # receives no production bonus unless the original query
                # overlaps locally.
                confidence = min(confidence, overlap)
            percentage *= max(0.0, min(1.0, confidence))
        adjustment = score * percentage
        item["authority_multiplier"] = 1.0 + percentage
        item["authority_adjustment"] = adjustment
        item["pre_authority_score"] = score
        item["score"] = score + adjustment
        ranked.append(item)
    ranked.sort(key=lambda row: (-float(row.get("score") or 0.0), int(row.get("fused_rank") or 10**9), str(row.get("path") or "")))
    return ranked


def _rank_source_roles(query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible helper retained for tests/extensions.

    R9 generalizes the old binary production/test bonus into authority-aware
    result focus while keeping the previous no-filter contract.
    """
    focus = _result_focus(query, "auto")["focus"]
    return _apply_authority_prior(query, rows, focus)


def _diversify_results(rows: list[dict[str, Any]], focus: str) -> list[dict[str, Any]]:
    """Deterministically reduce duplicate-role/path crowding without filtering.

    Implementation-focused search also reserves one top-three slot for a
    production implementation *only* when it already has independent retrieval
    support. This is representation, not a relevance shortcut: weak production
    code is never inserted merely because of its role.
    """
    remaining = [dict(row) for row in rows]
    selected: list[dict[str, Any]] = []
    path_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    while remaining:
        best_index = 0
        best_key: tuple[float, float, str] | None = None
        for idx, row in enumerate(remaining):
            score = float(row.get("score") or 0.0)
            path = str(row.get("path") or "")
            authority = str(row.get("authority_class") or _authority_class(row))
            penalty = 0.045 * path_counts.get(path, 0)
            if focus == "implementation":
                if authority in {"config_schema", "documentation", "generated_or_stub"}:
                    penalty += 0.025 * max(0, role_counts.get(authority, 0) - 1)
                if authority in {"test", "test_fixture"}:
                    penalty += 0.018 * max(0, role_counts.get(authority, 0) - 2)
            diversity_score = score - penalty
            key = (diversity_score, score, path)
            if best_key is None or key > best_key:
                best_key = key
                best_index = idx
        item = remaining.pop(best_index)
        path = str(item.get("path") or "")
        authority = str(item.get("authority_class") or _authority_class(item))
        item["pre_diversity_score"] = float(item.get("score") or 0.0)
        item["diversity_adjustment"] = float((best_key or (0.0, 0.0, ""))[0]) - float(item.get("score") or 0.0)
        item["score"] = float((best_key or (float(item.get("score") or 0.0), 0.0, ""))[0])
        selected.append(item)
        path_counts[path] = path_counts.get(path, 0) + 1
        role_counts[authority] = role_counts.get(authority, 0) + 1

    if focus == "implementation" and selected:
        top_pre = max(float(row.get("pre_authority_score") or row.get("pre_diversity_score") or row.get("score") or 0.0) for row in selected)
        top_window = min(3, len(selected))
        if not any(str(row.get("authority_class") or "") == "production_implementation" for row in selected[:top_window]):
            qualified: list[tuple[int, dict[str, Any]]] = []
            for idx, row in enumerate(selected[top_window:], start=top_window):
                if str(row.get("authority_class") or "") != "production_implementation":
                    continue
                baseline = float(row.get("pre_authority_score") or row.get("pre_diversity_score") or row.get("score") or 0.0)
                signal = float(row.get("authority_relevance_signal") or 0.0)
                if baseline >= top_pre * 0.35 and signal >= 0.70:
                    qualified.append((idx, row))
            if qualified:
                # Highest already-scored qualified implementation wins the
                # reserved slot. Preserve its score and mark the rank-only
                # diversity decision explicitly so telemetry stays honest.
                idx, promoted = max(qualified, key=lambda pair: (float(pair[1].get("score") or 0.0), -pair[0]))
                selected.pop(idx)
                promoted["authority_representation_reserved"] = True
                promoted["diversity_rank_reason"] = "qualified production implementation reserved within top three"
                selected.insert(top_window - 1, promoted)

    for rank, row in enumerate(selected, start=1):
        row["final_rank"] = rank
        row["final_score"] = float(row.get("score") or 0.0)
    return selected


def _compose_focus_results(
    rows: list[dict[str, Any]],
    focus: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply a bounded rank-only composition for implementation intent.

    Semantic relevance remains the admission gate. This stage can move a
    *qualified* concrete implementation ahead of descriptive tests/schema when
    its post-authority relevance is already close to the top semantic result.
    It never inserts weak production code merely to satisfy a role quota, and
    it never changes scores.
    """
    diag = diagnostics if diagnostics is not None else {}
    diag.clear()
    diag.update({
        "requested": focus == "implementation",
        "applied": False,
        "anchor_moved": False,
        "second_implementation_moved": False,
        "qualified_implementations": 0,
        "top_anchor_ratio": 0.82,
        "top_five_ratio": 0.68,
        "minimum_relevance_signal": 0.80,
        "moves": [],
    })
    selected = [dict(row) for row in rows]
    if focus != "implementation" or not selected:
        if focus != "implementation":
            diag["reason"] = "result focus is not implementation"
        return selected

    def basis(row: dict[str, Any]) -> float:
        return float(
            row.get("pre_diversity_score")
            or row.get("score")
            or row.get("pre_authority_score")
            or row.get("rank_fusion_score")
            or 0.0
        )

    top_basis = max(basis(row) for row in selected)
    if top_basis <= 0.0:
        diag["reason"] = "no positive relevance basis"
        return selected

    qualified: list[dict[str, Any]] = []
    for row in selected:
        if str(row.get("authority_class") or _authority_class(row)) != "production_implementation":
            continue
        signal = float(row.get("authority_relevance_signal") or 0.0)
        independent = bool(
            row.get("authority_dual_backend_support")
            or row.get("rerank_score_returned")
            or float(row.get("authority_query_overlap") or 0.0) >= 0.20
        )
        if (row.get("promotion_candidate_only") or row.get("refinement_candidate_only")) and not independent:
            continue
        if signal < 0.80 or not independent:
            continue
        item = row
        item["focus_composition_relevance_ratio"] = basis(row) / top_basis
        qualified.append(item)
    diag["qualified_implementations"] = len(qualified)
    if not qualified:
        diag["reason"] = "no independently relevant concrete production implementation qualified"
        return selected

    # Preserve original ranks for exact rank-movement telemetry.
    for idx, row in enumerate(selected, start=1):
        row["focus_composition_original_rank"] = idx

    best = max(qualified, key=lambda row: (basis(row), -int(row.get("final_rank") or 10**9)))
    best_ratio = basis(best) / top_basis
    if best_ratio >= 0.82:
        idx = next(i for i, row in enumerate(selected) if row is best)
        if idx > 0:
            moved = selected.pop(idx)
            moved["focus_composition_reason"] = "qualified concrete implementation reserved as implementation anchor"
            selected.insert(0, moved)
            diag["applied"] = True
            diag["anchor_moved"] = True
            diag["moves"].append({
                "symbol": moved.get("qualified_name") or moved.get("symbol_name"),
                "path": moved.get("path"),
                "from_rank": idx + 1,
                "to_rank": 1,
                "relevance_ratio": best_ratio,
                "reason": "implementation_anchor",
            })

    # If another independently strong concrete implementation is already close
    # to the semantic leaders, keep it inside the first five. This is conditional
    # representation, not an unconditional two-production quota.
    remaining_qualified = [row for row in qualified if row is not best]
    if remaining_qualified:
        second = max(remaining_qualified, key=lambda row: (basis(row), -int(row.get("final_rank") or 10**9)))
        second_ratio = basis(second) / top_basis
        idx = next(i for i, row in enumerate(selected) if row is second)
        if second_ratio >= 0.68 and idx >= 5:
            moved = selected.pop(idx)
            target = min(3, len(selected))
            moved["focus_composition_reason"] = "second independently relevant concrete implementation retained within top five"
            selected.insert(target, moved)
            diag["applied"] = True
            diag["second_implementation_moved"] = True
            diag["moves"].append({
                "symbol": moved.get("qualified_name") or moved.get("symbol_name"),
                "path": moved.get("path"),
                "from_rank": idx + 1,
                "to_rank": target + 1,
                "relevance_ratio": second_ratio,
                "reason": "implementation_top_five",
            })

    for rank, row in enumerate(selected, start=1):
        original_rank = int(row.get("focus_composition_original_rank") or rank)
        row["focus_composition_rank_adjustment"] = original_rank - rank
        row["final_rank"] = rank
        row["final_score"] = float(row.get("score") or 0.0)
    return selected


def _recommended_go_semantics_operations(query: str) -> list[str]:
    """Map narrow natural-language Go primitive intent to allow-listed probes.

    This is recommendation metadata only; it never executes code or filters
    repository evidence. Returning concrete operation names prevents the agent
    from having to guess the MCP operation after retrieval already established
    that a deterministic check is appropriate.
    """
    value = query.lower()
    operations: list[str] = []
    if re.search(r"\bpath\.join\b", value):
        operations.append("path_join")
    if re.search(r"\bpath\.clean\b", value):
        operations.append("path_clean")
    if re.search(r"\btime\.parseduration\b", value):
        operations.append("parse_duration")
    if re.search(r"\btime\.(?:duration|millisecond|microsecond|nanosecond|second|minute|hour)\b", value) and re.search(
        r"(?:\*|multipl(?:y|ied|ies|ication)|product|times)", value
    ):
        operations.append("duration_multiply")
    if re.search(r"\btype assertion\b", value) or re.search(r"\.\(error\)", value):
        operations.append("failed_error_type_assertion")
    if re.search(r"\bstrings\.replace\b", value):
        operations.append("strings_replace")
    if re.search(r"\burl\.parse\b", value):
        operations.append("url_parse")
    if (
        re.search(r"\bhttputil\.reverseproxy\b", value)
        or (re.search(r"\breverseproxy\b", value) and re.search(r"\bforwarded(?:-header| headers?)?\b", value))
    ):
        operations.append("reverse_proxy_rewrite_headers")
    # Stable order/deduplication keeps response metadata deterministic.
    return list(dict.fromkeys(operations))


def _needs_go_semantics_check(query: str) -> bool:
    return bool(_recommended_go_semantics_operations(query))


def _structural_promotions(
    db: Path,
    branch_key: str,
    rows: list[dict[str, Any]],
    query: str,
    *,
    max_sources: int = 10,
    max_promotions: int = 10,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    """Generate bounded production candidates from verified structural edges.

    Promotion is candidate generation only. A promoted candidate must still
    survive reranking/authority/diversity against the original user query.
    """
    promoted: list[dict[str, Any]] = []
    seen = {
        str(row.get("symbol_id") or row.get("chunk_id") or f"{row.get('path')}:{row.get('start_line')}")
        for row in rows
    }
    sources = [row for row in rows if _authority_class(row) in {"test", "test_fixture", "config_schema", "generated_or_stub"}]
    for source in sources[:max_sources]:
        source_symbol_id = str(source.get("symbol_id") or "")
        if not source_symbol_id:
            continue
        frontier: list[tuple[str, int, list[str]]] = [(source_symbol_id, 0, [])]
        visited = {source_symbol_id}
        while frontier and len(promoted) < max_promotions:
            current_id, depth, edge_ids = frontier.pop(0)
            if depth >= max_depth:
                continue
            for callee in store.callees(db, branch_key, current_id):
                target_id = str(callee.get("symbol_id") or "")
                if not target_id or target_id in visited:
                    continue
                if str(callee.get("resolution_status") or "") != "resolved":
                    continue
                visited.add(target_id)
                edge_id = str(callee.get("edge_id") or "")
                trace = edge_ids + ([edge_id] if edge_id else [])
                authority = _authority_class(callee)
                if authority in {"production_implementation", "production_helper"}:
                    key = target_id
                    if key not in seen:
                        item = dict(callee)
                        item["retrieval_backend"] = "structural_promotion"
                        item["retrieval_backends"] = ["structural_promotion"]
                        item["promotion_source_symbol_id"] = source_symbol_id
                        item["promotion_source_path"] = source.get("path")
                        item["promotion_source_rank"] = source.get("fused_rank")
                        item["promotion_edge"] = "resolved_call"
                        item["promotion_edge_ids"] = trace
                        item["promotion_graph_distance"] = depth + 1
                        item["promotion_candidate_only"] = True
                        item["promotion_query_overlap"] = _local_query_overlap(query, item)
                        # Structural connectivity is useful discovery evidence,
                        # not semantic proof. Keep the initial score deliberately
                        # small; a promoted candidate receives a reserved
                        # reranker slot below rather than inheriting the source
                        # candidate's relevance.
                        source_score = float(source.get("score") or source.get("fused_score") or 0.0)
                        overlap = float(item["promotion_query_overlap"])
                        distance_factor = 1.0 if depth == 0 else 0.65
                        item["score"] = max(0.001, (source_score * 0.08 + overlap * 0.12) * distance_factor)
                        item["raw_scores"] = {"structural_promotion": item["score"]}
                        seen.add(key)
                        promoted.append(item)
                        if len(promoted) >= max_promotions:
                            break
                frontier.append((target_id, depth + 1, trace))
            if len(promoted) >= max_promotions:
                break
        if len(promoted) >= max_promotions:
            break
    return promoted


_CONCRETE_SYMBOL_KINDS = {
    "function", "method", "constructor", "function_definition", "method_definition",
}
_REFINABLE_CONTAINER_KINDS = {"module", "file", "namespace", "class", "struct", "record", "type"}


def _symbol_refinements(
    db: Path,
    rows: list[dict[str, Any]],
    query: str,
    *,
    focus: str,
    max_parents: int = 8,
    max_children_per_parent: int = 8,
    max_total: int = 24,
    max_depth: int = 2,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand strong coarse production hits into concrete contained symbols.

    A parent module/file is discovery evidence, not implementation authority.
    Refinement gives its concrete children a bounded chance to be evaluated
    against the *original* query. Children never inherit the parent's semantic
    score or authority class as proof of relevance.
    """
    diag = diagnostics if diagnostics is not None else {}
    diag.clear()
    diag.update({
        "parents_considered": 0,
        "parents_eligible": 0,
        "parents_refined": 0,
        "parents_requalified": 0,
        "children_generated": 0,
        "children_available": 0,
        "children_already_present": 0,
        "children_requalified": 0,
        "children_omitted_by_parent_limit": 0,
        "children_omitted_by_total_limit": 0,
        "skipped": [],
        "parents": [],
    })
    if focus != "implementation" or not rows:
        diag["reason"] = "result focus is not implementation" if focus != "implementation" else "no discovery candidates"
        return []

    max_parents = max(1, min(max_parents, 20))
    max_children_per_parent = max(1, min(max_children_per_parent, 20))
    max_total = max(1, min(max_total, 60))
    max_depth = max(1, min(max_depth, 3))
    existing_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("symbol_id") or row.get("chunk_id") or f"{row.get('path')}:{row.get('start_line')}")
        if key:
            existing_rows.setdefault(key, row)
    existing = set(existing_rows)
    out: list[dict[str, Any]] = []
    parents_seen = 0

    for parent in rows:
        if parents_seen >= max_parents or len(out) >= max_total:
            break
        diag["parents_considered"] += 1
        if indexing_policy.source_role(str(parent.get("path") or "")) != "production":
            continue
        parent_kind = str(parent.get("symbol_kind") or parent.get("kind") or "").lower()
        parent_id = str(parent.get("symbol_id") or "")
        if parent_kind not in _REFINABLE_CONTAINER_KINDS:
            continue
        parents_seen += 1
        diag["parents_eligible"] += 1
        parent_diag: dict[str, Any] = {
            "path": parent.get("path"),
            "symbol_id": parent_id or None,
            "symbol_kind": parent_kind,
            "fused_rank": parent.get("fused_rank"),
            "children_generated": 0,
            "children_available": 0,
            "children_already_present": 0,
            "children_requalified": 0,
            "children_omitted_by_parent_limit": 0,
            "children_omitted_by_total_limit": 0,
            "represented_children": [],
            "omitted_children": [],
            "enumeration": [],
        }
        diag["parents"].append(parent_diag)
        frontier: list[tuple[str, int]] = [(parent_id, 0)]
        visited = {parent_id} if parent_id else set()
        parent_children = 0
        candidate_children: list[tuple[dict[str, Any], int, str]] = []

        # Prefer exact parent-child hierarchy when present.
        while frontier and parent_children < max_children_per_parent and len(out) < max_total:
            current_id, depth = frontier.pop(0)
            if not current_id or depth >= max_depth:
                continue
            for child in store.child_symbols(db, current_id, limit=max_children_per_parent * 4):
                child_id = str(child.get("symbol_id") or "")
                if not child_id or child_id in visited:
                    continue
                visited.add(child_id)
                child_kind = str(child.get("symbol_kind") or child.get("kind") or "").lower()
                if child_kind in _CONCRETE_SYMBOL_KINDS:
                    candidate_children.append((dict(child), depth + 1, "direct_child"))
                elif child_kind in _REFINABLE_CONTAINER_KINDS:
                    frontier.append((child_id, depth + 1))

        # R9.1.1: module/file refinement must not depend on grammar-specific
        # parent pointers. Go receiver methods and other language constructs can
        # be indexed in the same file while not appearing below the coarse
        # container through the direct-child API. Enumerate concrete symbols in
        # the exact indexed file as a deterministic fallback.
        if parent_kind in {"module", "file", "namespace"}:
            rel_path = str(parent.get("path") or "")
            project_id = str(parent.get("project_id") or "")
            branch_key = str(parent.get("branch_key") or "")
            if rel_path and project_id and branch_key:
                file_children = store.symbols_in_file(
                    db,
                    project_id,
                    branch_key,
                    rel_path,
                    kinds=_CONCRETE_SYMBOL_KINDS,
                    limit=max_children_per_parent * 8,
                )
                if file_children:
                    parent_diag["enumeration"].append("file_scope")
                for child in file_children:
                    child_id = str(child.get("symbol_id") or "")
                    if child_id and child_id not in {str(item[0].get("symbol_id") or "") for item in candidate_children}:
                        candidate_children.append((dict(child), 1, "file_scope"))

        if candidate_children and not parent_diag["enumeration"]:
            parent_diag["enumeration"].append("direct_child")

        # Deduplicate structural/file-scope enumeration before applying output
        # limits. Diagnostics distinguish a child that was already present in
        # broad discovery from one that was genuinely omitted by a bound.
        unique_children: list[tuple[dict[str, Any], int, str]] = []
        unique_child_keys: set[str] = set()
        for child, depth, enumeration in candidate_children:
            child_id = str(child.get("symbol_id") or "")
            key = child_id or str(child.get("chunk_id") or f"{child.get('path')}:{child.get('start_line')}")
            if not key or key in unique_child_keys:
                continue
            unique_child_keys.add(key)
            unique_children.append((child, depth, enumeration))
        candidate_children = unique_children
        parent_diag["children_available"] = len(candidate_children)
        diag["children_available"] += len(candidate_children)

        for child, depth, enumeration in candidate_children:
            child_id = str(child.get("symbol_id") or "")
            key = child_id or str(child.get("chunk_id") or f"{child.get('path')}:{child.get('start_line')}")
            child_label = str(child.get("qualified_name") or child.get("symbol_name") or child.get("name") or key)
            if not key:
                continue
            if key in existing:
                parent_diag["children_already_present"] += 1
                diag["children_already_present"] += 1
                parent_diag["represented_children"].append({
                    "symbol": child_label,
                    "reason": "already_in_discovery_pool",
                    "requalified": True,
                })
                # R9.1.3: an already-discovered concrete child still receives
                # the refinement relationship. Otherwise deduplication can
                # accidentally remove the exact candidate refinement intended
                # to give a fair reranker opportunity. This annotation is
                # admission metadata only; it never changes the child's score.
                existing_row = existing_rows.get(key)
                if existing_row is not None:
                    prior_parent_rank = int(existing_row.get("refinement_parent_fused_rank") or 10**9)
                    current_parent_rank = int(parent.get("fused_rank") or 10**9)
                    if not existing_row.get("refinement_requalified") or current_parent_rank < prior_parent_rank:
                        existing_row["refinement_requalified"] = True
                        existing_row["refinement_candidate_only"] = False
                        existing_row["refinement_parent_symbol_id"] = parent_id or None
                        existing_row["refinement_parent_path"] = parent.get("path")
                        existing_row["refinement_parent_fused_rank"] = parent.get("fused_rank")
                        existing_row["refinement_parent_score"] = float(parent.get("score") or parent.get("fused_score") or 0.0)
                        existing_row["refinement_parent_backends"] = list(parent.get("retrieval_backends") or [])
                        existing_row["refinement_depth"] = depth
                        existing_row["refinement_enumeration"] = enumeration
                        existing_row["refinement_reason"] = "concrete symbol already present in discovery pool and requalified by exact indexed production parent"
                        existing_row["refinement_query_overlap"] = _local_query_overlap(query, existing_row)
                        parent_diag["children_requalified"] += 1
                        diag["children_requalified"] += 1
                continue
            if parent_children >= max_children_per_parent:
                parent_diag["children_omitted_by_parent_limit"] += 1
                diag["children_omitted_by_parent_limit"] += 1
                parent_diag["omitted_children"].append({
                    "symbol": child_label,
                    "reason": "max_children_per_parent",
                })
                continue
            if len(out) >= max_total:
                parent_diag["children_omitted_by_total_limit"] += 1
                diag["children_omitted_by_total_limit"] += 1
                parent_diag["omitted_children"].append({
                    "symbol": child_label,
                    "reason": "max_total",
                })
                continue
            item = dict(child)
            overlap = _local_query_overlap(query, item)
            parent_score = float(parent.get("score") or parent.get("fused_score") or 0.0)
            item["retrieval_backend"] = "symbol_refinement"
            item["retrieval_backends"] = ["symbol_refinement"]
            item["refinement_candidate_only"] = True
            item["refinement_parent_symbol_id"] = parent_id or None
            item["refinement_parent_path"] = parent.get("path")
            item["refinement_parent_fused_rank"] = parent.get("fused_rank")
            item["refinement_parent_score"] = parent_score
            item["refinement_parent_backends"] = list(parent.get("retrieval_backends") or [])
            item["refinement_depth"] = depth
            item["refinement_enumeration"] = enumeration
            item["refinement_reason"] = "concrete symbol contained in the exact indexed production container/file"
            item["refinement_query_overlap"] = overlap
            # Keep discovery-only score intentionally small. The child earns
            # material movement only through its own independent rerank/query
            # evidence in later stages.
            item["score"] = max(0.001, parent_score * 0.06 + overlap * 0.08)
            item["raw_scores"] = {"symbol_refinement": item["score"]}
            existing.add(key)
            out.append(item)
            parent_children += 1
            parent_diag["children_generated"] += 1

        if parent_children:
            diag["parents_refined"] += 1
        if parent_diag["children_requalified"]:
            diag["parents_requalified"] += 1
        if not parent_children and not parent_diag["children_requalified"]:
            diag["skipped"].append({
                "path": parent.get("path"),
                "fused_rank": parent.get("fused_rank"),
                "reason": "eligible coarse production container had no concrete indexed children",
            })
        diag["children_generated"] = len(out)
    return out


def _compose_rerank_candidates(
    discovery_rows: list[dict[str, Any]],
    promotions: list[dict[str, Any]],
    candidate_limit: int,
    *,
    refinements: list[dict[str, Any]] | None = None,
    evaluation_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Reserve bounded reranker capacity for structural/refinement candidates.

    Expansions must be *evaluated* rather than artificially scored high enough
    to displace raw retrieval. Crucially, reserved candidates are placed inside
    the remote reranker's actual candidate window, not merely appended to a
    larger post-rerank pool where they would never receive a score.
    """
    refinements = list(refinements or [])
    expansions = refinements + list(promotions)
    if not expansions:
        return [dict(row) for row in discovery_rows[:candidate_limit]]
    candidate_limit = max(1, min(candidate_limit, 200))
    eval_window = max(1, min(int(evaluation_limit or candidate_limit), candidate_limit))
    quota = min(len(expansions), max(2, min(12, max(1, eval_window // 3))))
    raw_in_eval = max(1, eval_window - quota)
    out = [dict(row) for row in discovery_rows[:raw_in_eval]]
    seen = {
        str(row.get("symbol_id") or row.get("chunk_id") or f"{row.get('path')}:{row.get('start_line')}")
        for row in out
    }
    for row in expansions:
        key = str(row.get("symbol_id") or row.get("chunk_id") or f"{row.get('path')}:{row.get('start_line')}")
        if key in seen:
            continue
        out.append(dict(row))
        seen.add(key)
        if len(out) >= eval_window:
            break
    # Preserve raw candidates that fell outside the reranker evaluation window
    # so final composition can still use their fused evidence.
    for row in discovery_rows[raw_in_eval:]:
        if len(out) >= candidate_limit:
            break
        key = str(row.get("symbol_id") or row.get("chunk_id") or f"{row.get('path')}:{row.get('start_line')}")
        if key in seen:
            continue
        out.append(dict(row))
        seen.add(key)

    # R9.1.4: refinement can requalify a concrete child that already existed
    # deep in broad discovery. The expansion reservation above can otherwise
    # consume enough bounded-pool capacity to evict that exact original row
    # before _select_rerank_window() gets a chance to evaluate it. Preserve
    # every such requalified discovery candidate without changing its score or
    # broad rank: replace the lowest-priority unprotected row at the bounded
    # tail, then append the protected candidate at the tail. Reserved generated
    # refinements/promotions and other requalified rows are never chosen as
    # eviction victims. Selection lanes still decide whether the preserved row
    # receives one of the finite remote-reranker slots.
    protected = [row for row in discovery_rows if row.get("refinement_requalified")]
    for row in protected:
        key = _rerank_candidate_key(row)
        if not key or key in seen:
            continue
        if len(out) < candidate_limit:
            item = dict(row)
            item["rerank_composition_protected"] = True
            item["rerank_composition_protection_reason"] = "existing concrete child requalified by structural refinement"
            out.append(item)
            seen.add(key)
            continue
        victim_index = None
        for index in range(len(out) - 1, -1, -1):
            candidate = out[index]
            if candidate.get("refinement_requalified"):
                continue
            if candidate.get("refinement_candidate_only") or candidate.get("promotion_candidate_only"):
                continue
            victim_index = index
            break
        if victim_index is None:
            # This can only occur when the bounded pool is entirely composed of
            # protected/reserved candidates. Do not exceed candidate_limit.
            continue
        victim = out.pop(victim_index)
        victim_key = _rerank_candidate_key(victim)
        if victim_key:
            seen.discard(victim_key)
        item = dict(row)
        item["rerank_composition_protected"] = True
        item["rerank_composition_protection_reason"] = "existing concrete child requalified by structural refinement"
        out.append(item)
        seen.add(key)
    return out


def _rank_fuse_after_rerank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Combine broad retrieval and reranker order without mixing raw scales."""
    if not rows:
        return []
    try:
        k = max(10, min(int(os.environ.get("AWOKI_CODE_RERANK_RRF_K", "40")), 200))
    except ValueError:
        k = 40
    try:
        rerank_weight = max(0.1, min(float(os.environ.get("AWOKI_CODE_RERANK_RRF_WEIGHT", "1.25")), 5.0))
    except ValueError:
        rerank_weight = 1.25

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        fused_rank = int(item.get("fused_rank") or 0)
        parent_fused_rank = int(item.get("refinement_parent_fused_rank") or item.get("promotion_source_rank") or 0)
        rerank_rank = int(item.get("rerank_rank") or 0)
        components: dict[str, float] = {}
        score = 0.0
        if fused_rank > 0:
            components["fused"] = 1.0 / (k + fused_rank)
            score += components["fused"]
        elif parent_fused_rank > 0 and rerank_rank > 0 and item.get("rerank_score") is not None:
            # Parent relevance lets an expansion enter evaluation, but a child
            # receives only bounded discovery credit and must earn its own
            # reranker position to become competitive.
            parent_weight = 0.45 if item.get("refinement_candidate_only") else 0.35
            components["parent_discovery"] = parent_weight / (k + parent_fused_rank)
            score += components["parent_discovery"]
        if rerank_rank > 0 and item.get("rerank_score") is not None:
            components["rerank"] = rerank_weight / (k + rerank_rank)
            score += components["rerank"]
        # Rows outside the reranker's scored set retain broad retrieval order.
        # For an expansion with no rerank position, its deliberately tiny
        # candidate-generation score is kept as a bounded last-resort signal.
        if score <= 0.0:
            score = float(item.get("score") or 0.0)
            components["fallback"] = score
        item["pre_rank_fusion_score"] = float(item.get("score") or 0.0)
        item["rank_fusion_components"] = components
        item["rank_fusion_score"] = score
        item["score"] = score
        out.append(item)
    out.sort(key=lambda row: (
        -float(row.get("score") or 0.0),
        int(row.get("rerank_rank") or 10**9),
        int(row.get("fused_rank") or row.get("refinement_parent_fused_rank") or row.get("promotion_source_rank") or 10**9),
        str(row.get("path") or ""),
    ))
    return out


def _rerank_candidate_key(row: dict[str, Any]) -> str:
    return str(row.get("chunk_id") or row.get("symbol_id") or f"{row.get('path')}:{row.get('start_line')}")


def _select_rerank_window(
    query: str,
    rows: list[dict[str, Any]],
    selected_count: int,
    *,
    focus: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Select the remote-reranker window with bounded focus/refinement lanes.

    R9.1.3 keeps the strongest broad discovery candidates while reserving a
    small, deterministic share of the finite reranker budget for candidates
    that are easy to lose behind descriptive tests/schema: concrete
    implementations for implementation focus, tests for test focus, and
    structurally refined/requalified candidates. Selection only grants a
    reranker opportunity; it never changes relevance scores or final ranks.
    """
    selected_count = max(0, min(int(selected_count), len(rows)))
    pool = [dict(row) for row in rows]
    if selected_count <= 0:
        return [], pool, {
            "pool_size": len(pool), "budget": 0,
            "general_budget": 0, "focus_budget": 0, "structural_budget": 0,
            "general_selected": 0, "focus_selected": 0, "refined_selected": 0,
            "deduplicated": 0, "refill_selected": 0,
            "refill_rejected_low_relevance": 0, "unused_budget": 0,
        }

    special_focus = focus in {"implementation", "tests", "config"}
    if special_focus and selected_count >= 6:
        general_budget = max(1, int(round(selected_count * 0.60)))
        if focus == "implementation":
            focus_budget = max(1, int(round(selected_count * 0.27)))
            structural_budget = max(0, selected_count - general_budget - focus_budget)
        else:
            # Explicit test/config intent has no implementation-refinement lane
            # in the normal pipeline, so spend the remaining budget directly
            # on the requested source role instead of wasting reserved capacity.
            focus_budget = max(1, selected_count - general_budget)
            structural_budget = 0
    else:
        general_budget = selected_count
        focus_budget = 0
        structural_budget = 0

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    deduplicated = 0

    def add(row: dict[str, Any], lane: str, reason: str) -> bool:
        nonlocal deduplicated
        key = _rerank_candidate_key(row)
        if not key or key in selected_keys:
            deduplicated += 1
            return False
        item = dict(row)
        item["rerank_selection_lane"] = lane
        item["rerank_selection_reason"] = reason
        selected.append(item)
        selected_keys.add(key)
        return True

    for row in pool[:general_budget]:
        add(row, "general", "top broad fused/refined discovery candidate")

    def candidate_rank(row: dict[str, Any]) -> int:
        return int(row.get("pre_rerank_rank") or row.get("fused_rank") or 10**9)

    def parent_rank(row: dict[str, Any]) -> int:
        return int(row.get("refinement_parent_fused_rank") or row.get("promotion_source_rank") or 10**9)

    def dual_support(row: dict[str, Any]) -> bool:
        backends = set(str(value) for value in (row.get("retrieval_backends") or []) if value)
        if row.get("retrieval_backend"):
            backends.add(str(row.get("retrieval_backend")))
        return {"code_fts", "code_qdrant"}.issubset(backends)

    def backend_rank(row: dict[str, Any], backend: str) -> int:
        value = row.get(f"{backend}_rank")
        try:
            rank = int(value or 0)
        except (TypeError, ValueError):
            return 10**9
        return rank if rank > 0 else 10**9

    def focus_overlap(row: dict[str, Any]) -> float:
        return float(
            row.get("refinement_query_overlap")
            or _local_query_overlap(
                query,
                row,
                extra_stop=_FOCUS_ROLE_QUERY_STOP.get(focus, set()),
            )
            or 0.0
        )

    def requested_role_relevance_signals(row: dict[str, Any]) -> list[str]:
        """Independent evidence required before role-only focus reservation.

        Explicit ``tests``/``config`` intent narrows the desired source role,
        but role membership is not relevance. A deep reserved-lane candidate
        must independently connect to the query through lexical overlap, a
        strong backend rank, or corroborated dual retrieval. This prevents
        arbitrary ``*_test`` or config files from consuming scarce reranker
        slots merely because their source role matches the request.
        """
        signals: list[str] = []
        overlap = focus_overlap(row)
        fts_rank = backend_rank(row, "fts")
        qdrant_rank = backend_rank(row, "qdrant")
        if overlap >= 0.10:
            signals.append("query_overlap")
        # Preserve semantic-only rescues and lexical-only rescues when they are
        # independently strong enough. Thresholds scale from the finite
        # reranker window rather than repository/language-specific constants.
        if fts_rank <= max(12, selected_count // 2):
            signals.append("strong_fts_rank")
        if qdrant_rank <= max(10, selected_count // 3):
            signals.append("strong_qdrant_rank")
        # Dual support is useful corroboration, but only when at least one
        # backend is itself within a bounded relevance neighborhood. Two weak
        # backend appearances must not manufacture a reserved focus slot.
        if dual_support(row) and min(fts_rank, qdrant_rank) <= max(24, selected_count):
            signals.append("dual_fts_qdrant_support")
        return signals

    def refill_relevance_signals(row: dict[str, Any]) -> list[str]:
        """Return evidence that justifies spending otherwise-unused capacity."""
        signals: list[str] = []
        overlap = _local_query_overlap(query, row)
        fts_rank = backend_rank(row, "fts")
        qdrant_rank = backend_rank(row, "qdrant")
        if overlap >= 0.10:
            signals.append("query_overlap")
        if fts_rank <= max(18, selected_count):
            signals.append("strong_fts_rank")
        if qdrant_rank <= max(12, selected_count // 2):
            signals.append("strong_qdrant_rank")
        if dual_support(row) and min(fts_rank, qdrant_rank) <= max(30, selected_count):
            signals.append("dual_fts_qdrant_support")
        if row.get("refinement_requalified") or row.get("refinement_candidate_only") or row.get("promotion_candidate_only"):
            if parent_rank(row) <= max(18, selected_count):
                signals.append("strong_refined_or_requalified_parent")
        return signals

    focus_candidates: list[dict[str, Any]] = []
    for row in pool:
        authority = str(row.get("authority_class") or _authority_class(row))
        if focus == "implementation" and authority == "production_implementation":
            overlap = float(row.get("refinement_query_overlap") or _local_query_overlap(query, row) or 0.0)
            strong_parent = bool(
                (row.get("refinement_requalified") or row.get("refinement_candidate_only"))
                and parent_rank(row) <= max(12, selected_count)
            )
            signals = []
            if strong_parent:
                signals.append("strong_refined_or_requalified_parent")
            if dual_support(row):
                signals.append("dual_fts_qdrant_support")
            if overlap >= 0.12:
                signals.append("query_overlap")
            row["rerank_focus_lane_eligible"] = bool(signals)
            row["rerank_focus_lane_signals"] = signals
            if signals:
                focus_candidates.append(row)
        elif focus == "tests" and authority in {"test", "test_fixture"}:
            relevance_signals = requested_role_relevance_signals(row)
            row["rerank_focus_lane_eligible"] = bool(relevance_signals)
            row["rerank_focus_lane_signals"] = ["requested_test_role"] + relevance_signals
            if relevance_signals:
                focus_candidates.append(row)
        elif focus == "config" and authority == "config_schema":
            relevance_signals = requested_role_relevance_signals(row)
            row["rerank_focus_lane_eligible"] = bool(relevance_signals)
            row["rerank_focus_lane_signals"] = ["requested_config_role"] + relevance_signals
            if relevance_signals:
                focus_candidates.append(row)
        else:
            row["rerank_focus_lane_eligible"] = False
            row["rerank_focus_lane_signals"] = []

    def focus_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        overlap = focus_overlap(row) if focus in {"tests", "config"} else float(
            row.get("refinement_query_overlap") or _local_query_overlap(query, row) or 0.0
        )
        requalified = bool(row.get("refinement_requalified"))
        structural = bool(row.get("refinement_candidate_only") or row.get("promotion_candidate_only"))
        if focus in {"tests", "config"}:
            signals = set(str(value) for value in (row.get("rerank_focus_lane_signals") or []))
            evidence_strength = (
                (3 if "query_overlap" in signals else 0)
                + (2 if "strong_qdrant_rank" in signals else 0)
                + (2 if "strong_fts_rank" in signals else 0)
                + (1 if "dual_fts_qdrant_support" in signals else 0)
            )
            return (
                -evidence_strength,
                -overlap,
                min(backend_rank(row, "fts"), backend_rank(row, "qdrant")),
                candidate_rank(row),
                str(row.get("path") or ""),
            )
        # A concrete child of a strong already-discovered parent deserves a
        # fair semantic evaluation even when its own broad rank fell outside
        # the global cutoff. Dual FTS+Qdrant support and local overlap are the
        # next strongest admission signals. This ordering affects evaluation
        # opportunity only, never the final result score.
        return (
            0 if requalified else 1,
            parent_rank(row) if (requalified or structural) else 10**9,
            0 if dual_support(row) else 1,
            -overlap,
            candidate_rank(row),
            str(row.get("path") or ""),
        )

    ordered_focus_candidates = sorted(focus_candidates, key=focus_sort_key)
    for focus_order, row in enumerate(ordered_focus_candidates, start=1):
        row["rerank_focus_selection_order"] = focus_order

    focus_selected = 0
    for row in ordered_focus_candidates:
        if focus_selected >= focus_budget or len(selected) >= selected_count:
            break
        if add(row, "focus", f"candidate matches result_focus={focus} and has independent relevance evidence"):
            focus_selected += 1

    structural_candidates = [
        row for row in pool
        if row.get("refinement_candidate_only")
        or row.get("refinement_requalified")
        or row.get("promotion_candidate_only")
    ]
    structural_keys = {_rerank_candidate_key(row) for row in structural_candidates}
    for row in pool:
        row["rerank_structural_lane_eligible"] = _rerank_candidate_key(row) in structural_keys
    structural_candidates.sort(key=lambda row: (
        parent_rank(row),
        -float(row.get("refinement_query_overlap") or row.get("promotion_query_overlap") or _local_query_overlap(query, row) or 0.0),
        candidate_rank(row),
        str(row.get("path") or ""),
    ))
    for structural_order, row in enumerate(structural_candidates, start=1):
        row["rerank_structural_selection_order"] = structural_order
    refined_selected = 0
    for row in structural_candidates:
        if refined_selected >= structural_budget or len(selected) >= selected_count:
            break
        if add(row, "refined", "structural refinement/promotion candidate reserved for independent rerank evaluation"):
            refined_selected += 1

    refill_selected = 0
    refill_rejected_low_relevance = 0
    for row in pool:
        if len(selected) >= selected_count:
            break
        if _rerank_candidate_key(row) in selected_keys:
            continue
        refill_signals = refill_relevance_signals(row)
        row["rerank_refill_relevance_signals"] = refill_signals
        if not refill_signals:
            refill_rejected_low_relevance += 1
            continue
        if add(row, "refill", "unused lane capacity refilled by independently relevant broad candidate"):
            selected[-1]["rerank_refill_relevance_signals"] = list(refill_signals)
            refill_selected += 1

    tail: list[dict[str, Any]] = []
    for row in pool:
        if _rerank_candidate_key(row) in selected_keys:
            continue
        item = dict(row)
        item["rerank_selection_lane"] = "not_selected"
        exclusion_reasons: list[str] = []
        if item.get("rerank_focus_lane_eligible"):
            exclusion_reasons.append("focus budget exhausted by higher-priority eligible candidates")
        if item.get("rerank_structural_lane_eligible"):
            exclusion_reasons.append("structural budget exhausted by higher-priority eligible candidates")
        if not exclusion_reasons:
            exclusion_reasons.append("outside general/refill cutoff and not eligible for a reserved lane")
        item["rerank_selection_exclusion"] = "; ".join(exclusion_reasons)
        item["rerank_selection_reason"] = item["rerank_selection_exclusion"]
        tail.append(item)

    # General-lane rows were copied before focus/structural eligibility was
    # computed. Synchronize the observability-only annotations back onto every
    # selected copy so diagnostics never claim a candidate was ineligible merely
    # because it was already safely admitted by the broad lane.
    pool_by_key = {_rerank_candidate_key(row): row for row in pool}
    for item in selected:
        source = pool_by_key.get(_rerank_candidate_key(item)) or {}
        for key in (
            "rerank_focus_lane_eligible",
            "rerank_focus_lane_signals",
            "rerank_focus_selection_order",
            "rerank_structural_lane_eligible",
            "rerank_structural_selection_order",
        ):
            if key in source:
                item[key] = source[key]

    telemetry = {
        "pool_size": len(pool),
        "budget": selected_count,
        "general_budget": general_budget,
        "focus_budget": focus_budget,
        "structural_budget": structural_budget,
        "general_selected": sum(1 for row in selected if row.get("rerank_selection_lane") == "general"),
        "focus_selected": focus_selected,
        "refined_selected": refined_selected,
        "deduplicated": deduplicated,
        "refill_selected": refill_selected,
        "refill_rejected_low_relevance": refill_rejected_low_relevance,
        "unused_budget": max(0, selected_count - len(selected)),
    }
    return selected, tail, telemetry


def _rerank(
    query: str,
    rows: list[dict[str, Any]],
    limit: int,
    *,
    enabled: bool = True,
    focus: str = "balanced",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profile = rag_backend.rerank_profile()
    selected_count = min(len(rows), int(profile.get("candidate_limit") or len(rows)))
    selected_rows, all_tail_rows, selection_telemetry = _select_rerank_window(
        query, rows, selected_count, focus=focus,
    )
    selected_count = len(selected_rows)
    telemetry: dict[str, Any] = {
        "requested": bool(enabled),
        "configured": bool(profile.get("enabled")),
        "attempted": False,
        "applied": False,
        "backend": str(profile.get("provider") or ""),
        "model": str(profile.get("model") or ""),
        "candidates_in": selected_count,
        "results_out": 0,
        "candidates_selected": selected_count,
        "request_documents": selected_count,
        "scores_returned": 0,
        "scores_returned_to_awoki": 0,
        "configured_top_n": min(selected_count, int(profile.get("top_n") or selected_count)),
        # Result-first internal reranking needs one score per selected candidate
        # whenever the backend contract can provide it. Awoki, not a top_n=10
        # transport truncation, decides which candidates survive later stages.
        "results_requested_top_n": selected_count,
        "candidates_scored": 0,
        # Compatibility field. This means Awoki did not receive an explicit
        # score for the selected candidate; it does NOT claim the remote model
        # failed to evaluate that document internally.
        "candidates_unscored": selected_count,
        "selected_without_returned_score": selected_count,
        "backend_scoring_coverage": "not_observable_from_rerank_contract",
        "candidates_not_selected": max(0, len(rows) - selected_count),
        "post_rerank_pool_size": len(rows[:limit]),
        "latency_ms": 0,
        "reason": "",
        "selection": selection_telemetry,
    }
    if not enabled:
        telemetry["reason"] = "reranker disabled by query controls"
        return rows[:limit], telemetry
    if not rag_backend.rerank_enabled():
        telemetry["reason"] = "reranker is not configured/enabled"
        return rows[:limit], telemetry
    if not rows:
        telemetry["reason"] = "no candidates to rerank"
        return [], telemetry
    tail_rows = [dict(row) for row in all_tail_rows[: max(0, limit - selected_count)]]
    payload = []
    row_map: dict[str, dict[str, Any]] = {}
    for row in _annotate_authority(selected_rows):
        key = str(row.get("chunk_id") or row.get("symbol_id") or f"{row.get('path')}:{row.get('start_line')}")
        row_map[key] = row
        payload.append({
            "id": key,
            "title": str(row.get("title") or row.get("qualified_name") or row.get("path") or ""),
            # Keep semantic relevance and authority as separate stages. The
            # reranker sees source/symbol shape, not Awoki's later authority
            # judgment, so it cannot simply echo a production/test preference.
            "kind": f"code symbol_kind={row.get('symbol_kind') or row.get('kind') or 'unknown'}",
            "source_path": str(row.get("path") or ""),
            "preview": str(row.get("text") or "")[:4000],
            "score": float(row.get("score") or 0.0),
        })
    # Code-search used to impose its own 5s reranker ceiling, even when the
    # operator configured the shared reranker timeout to a higher value. That
    # made healthy TEI backends appear flaky on 30-document windows. Inherit
    # the configured reranker timeout unless a code-search-specific override is
    # explicitly set. Keep a hard local ceiling so a broken endpoint cannot
    # stall an MCP call indefinitely.
    profile_timeout = float(rag_backend.rerank_profile().get("timeout_seconds") or 20.0)
    raw_timeout = os.environ.get("AWOKI_CODE_RERANK_TIMEOUT_SECONDS", "").strip()
    try:
        requested_code_timeout = float(raw_timeout) if raw_timeout else profile_timeout
    except ValueError:
        requested_code_timeout = profile_timeout
    # rag_backend.rerank_hits treats timeout_override as an upper bound on the
    # shared reranker timeout. Mirror that exact effective value in telemetry so
    # captured evidence never claims a timeout budget the HTTP client did not use.
    rerank_timeout = max(0.25, min(60.0, profile_timeout, requested_code_timeout))
    telemetry["timeout_seconds"] = rerank_timeout
    telemetry["timeout_source"] = (
        "AWOKI_CODE_RERANK_TIMEOUT_SECONDS<=AWOKI_RERANK_TIMEOUT_SECONDS"
        if raw_timeout else "AWOKI_RERANK_TIMEOUT_SECONDS"
    )
    started = time.monotonic()
    telemetry["attempted"] = True
    try:
        reranked = rag_backend.rerank_hits(
            query,
            payload,
            limit=selected_count,
            timeout_override=rerank_timeout,
            top_n_override=selected_count,
        )
    except Exception as exc:
        telemetry["latency_ms"] = int((time.monotonic() - started) * 1000)
        telemetry["reason"] = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    telemetry["latency_ms"] = int((time.monotonic() - started) * 1000)
    out: list[dict[str, Any]] = []
    scored_count = 0
    for rank, item in enumerate(reranked, start=1):
        original = dict(row_map.get(str(item.get("id")), {}))
        if not original:
            continue
        if "rerank_score" in item:
            original["pre_rerank_score"] = original.get("score")
            original["rerank_score"] = item["rerank_score"]
            original["rerank_backend"] = item.get("rerank_backend")
            scored_count += 1
            original["rerank_rank"] = scored_count
            original["rerank_attempted_for_candidate"] = True
            original["rerank_selected"] = True
            original["rerank_score_returned"] = True
            original["rerank_scored"] = True
            telemetry["applied"] = True
        else:
            original["rerank_attempted_for_candidate"] = True
            original["rerank_selected"] = True
            original["rerank_score_returned"] = False
            original["rerank_scored"] = False
        if item.get("rerank_error"):
            original["rerank_error"] = item["rerank_error"]
            original["rerank_fallback"] = True
            telemetry["reason"] = str(item.get("rerank_error") or "")[:1000]
        out.append(original)
    seen_keys = {
        str(row.get("chunk_id") or row.get("symbol_id") or f"{row.get('path')}:{row.get('start_line')}")
        for row in out
    }
    # A conforming backend should return every selected candidate, even when
    # only top_n carry explicit scores. Preserve any omitted selected rows so
    # no remote response can silently delete discovery evidence.
    for row in selected_rows:
        key = str(row.get("chunk_id") or row.get("symbol_id") or f"{row.get('path')}:{row.get('start_line')}")
        if key in seen_keys:
            continue
        item = dict(row)
        item["rerank_attempted_for_candidate"] = True
        item["rerank_selected"] = True
        item["rerank_score_returned"] = False
        item["rerank_scored"] = False
        out.append(item)
        seen_keys.add(key)
    for row in tail_rows:
        item = dict(row)
        item["rerank_attempted_for_candidate"] = False
        item["rerank_selected"] = False
        item["rerank_score_returned"] = False
        item["rerank_scored"] = False
        out.append(item)

    out = _rank_fuse_after_rerank(out[:limit])
    telemetry["scores_returned"] = scored_count
    telemetry["scores_returned_to_awoki"] = scored_count
    telemetry["candidates_scored"] = scored_count
    telemetry["candidates_unscored"] = max(0, selected_count - scored_count)
    telemetry["selected_without_returned_score"] = max(0, selected_count - scored_count)
    telemetry["post_rerank_pool_size"] = len(out)
    # Legacy field now means explicit scored reranker results, not the complete
    # post-rerank candidate pool. The latter is separately exposed above.
    telemetry["results_out"] = scored_count
    if telemetry["applied"] and not telemetry["reason"]:
        telemetry["reason"] = "reranker returned explicit scores"
    elif telemetry["attempted"] and not telemetry["applied"] and not telemetry["reason"]:
        telemetry["reason"] = "reranker attempt returned no explicit scores"

    reason_lower = str(telemetry.get("reason") or "").lower()
    failure_class = "none"
    retryable = False
    if telemetry.get("attempted") and not telemetry.get("applied"):
        if "timeout" in reason_lower or "timed out" in reason_lower:
            failure_class, retryable = "timeout", True
        elif "429" in reason_lower or "rate limit" in reason_lower or "too many requests" in reason_lower:
            failure_class, retryable = "rate_limited", True
        elif any(token in reason_lower for token in ("connection reset", "connecterror", "connection refused", "network")):
            failure_class, retryable = "transport", True
        elif any(token in reason_lower for token in ("500", "502", "503", "504", "server error")):
            failure_class, retryable = "server", True
        elif any(token in reason_lower for token in ("401", "403", "unauthorized", "forbidden", "credential")):
            failure_class = "auth"
        elif any(token in reason_lower for token in ("response", "json", "usable index/score", "protocol")):
            failure_class = "protocol"
        else:
            failure_class = "unknown"
    telemetry["failure_class"] = failure_class
    telemetry["retryable"] = retryable
    telemetry["degraded"] = bool(telemetry.get("attempted") and not telemetry.get("applied"))
    return out[:limit], telemetry


def _bounded_rows(db: Path, rows: list[dict[str, Any]], view: str, limit: int, max_chars: int | None = None) -> list[dict[str, Any]]:
    view = (view or "context").lower()
    if view not in {"peek", "context", "full", "diagnostics"}:
        view = "context"
    if view == "diagnostics":
        # Diagnostics are for pipeline observability; the full bounded candidate
        # pool is available through the trace handle. Keep only the final top 10
        # inline so acceptance/debug responses stay below common MCP transport
        # truncation thresholds while still supporting top-10 quality checks.
        return _diagnostic_rows(rows, min(limit, 10))
    if max_chars is None or max_chars <= 0:
        max_chars = {
            "peek": int(os.environ.get("AWOKI_CODE_PEEK_MAX_CHARS", "3000")),
            "context": int(os.environ.get("AWOKI_CODE_CONTEXT_MAX_CHARS", "16000")),
            "full": int(os.environ.get("AWOKI_CODE_FULL_MAX_CHARS", "50000")),
        }[view]
    candidates = rows[: max(1, min(limit, 50))]
    remaining = max(500, max_chars)
    # Result metadata is part of retrieval correctness. Never drop a ranked hit
    # merely because earlier previews consumed the display budget; instead share
    # the preview budget across the requested top-K.
    per_hit_budget = max(120, remaining // max(1, len(candidates)))
    output: list[dict[str, Any]] = []
    for index, row in enumerate(candidates):
        symbol_name = str(row.get("symbol_name") or row.get("name") or "")
        symbol_kind = str(row.get("symbol_kind") or row.get("kind") or "")
        text = str(row.get("text") or "")
        if view == "peek":
            preview = ""
            budget = 0
        elif view == "full" and row.get("symbol_id"):
            parts = store.symbol_chunks(db, str(row["symbol_id"]))
            text = "\n".join(str(part.get("text") or "") for part in parts)
            remaining_hits = max(1, len(candidates) - index)
            fair_share = max(120, remaining // remaining_hits)
            budget = min(len(text), fair_share, 20000, max(0, remaining))
            preview = text[:budget]
        else:
            remaining_hits = max(1, len(candidates) - index)
            fair_share = max(120, remaining // remaining_hits)
            budget = min(2500, per_hit_budget, fair_share, max(0, remaining))
            preview = text[:budget]
        backends = [
            str(value) for value in (row.get("retrieval_backends") or [row.get("retrieval_backend")])
            if value
        ]
        reason_by_backend = {
            "definition_index": "exact structural definition candidate",
            "exact_structural": "exact symbol, path, or source occurrence",
            "code_fts": "lexical match in the structural code index",
            "code_qdrant": "semantic match in the structural code index",
            "call_graph_callers": "static caller edge",
            "call_graph_callees": "static callee edge",
            "call_graph_path": "resolved edge on a bounded static path",
            "structural_promotion": "production candidate reached through a verified bounded structural edge",
            "symbol_refinement": "concrete symbol refined from a coarse production container and independently evaluated",
        }
        item = {
            "kind": "code",
            "project_id": row.get("project_id"),
            "repo_id": row.get("repo_id"),
            "source_id": row.get("source_id") or row.get("repo_id"),
            "source_type": row.get("source_type") or ("git" if row.get("repo_id") else "directory"),
            "revision_key": row.get("revision_key") or row.get("branch_key"),
            "content_identity": row.get("content_identity") or row.get("commit_sha"),
            "branch_key": row.get("branch_key"),
            "branch_name": row.get("branch_name"),
            "commit_sha": row.get("commit_sha"),
            "dirty": bool(row.get("dirty")),
            "path": row.get("path"),
            "source_role": indexing_policy.source_role(str(row.get("path") or "")),
            "language": row.get("language"),
            "parser_id": row.get("parser_id"),
            "parse_mode": row.get("parse_mode"),
            "parse_status": row.get("parse_status"),
            "parse_diagnostics": (
                json.loads(str(row.get("diagnostics_json") or "[]"))
                if str(row.get("diagnostics_json") or "").strip().startswith("[")
                else []
            ),
            "symbol_id": row.get("symbol_id"),
            "symbol": symbol_name,
            "qualified_name": row.get("qualified_name"),
            "symbol_kind": symbol_kind,
            "candidate_level": _candidate_level(row),
            "start_line": int(row.get("start_line") or 1),
            "end_line": int(row.get("end_line") or row.get("start_line") or 1),
            "signature": str(row.get("signature") or ""),
            "preview": preview,
            "truncated": bool(text and len(preview) < len(text)),
            "score": float(row.get("score") or 0.0),
            "final_rank": row.get("final_rank"),
            "final_score": row.get("final_score"),
            "fused_rank": row.get("fused_rank"),
            "fused_score": row.get("fused_score"),
            "fts_rank": row.get("fts_rank"),
            "fts_raw_score": row.get("fts_raw_score"),
            "lexical_match_mode": row.get("lexical_match_mode"),
            "lexical_normalization_terms": list(row.get("lexical_normalization_terms") or []),
            "qdrant_rank": row.get("qdrant_rank"),
            "qdrant_raw_score": row.get("qdrant_raw_score"),
            "pre_rerank_rank": row.get("pre_rerank_rank"),
            "rerank_rank": row.get("rerank_rank"),
            "rerank_score": row.get("rerank_score"),
            "rerank_attempted_for_candidate": bool(row.get("rerank_attempted_for_candidate")),
            "rerank_selected": bool(row.get("rerank_selected")),
            "rerank_score_returned": bool(row.get("rerank_score_returned")),
            "rerank_scored": bool(row.get("rerank_scored")),
            "rerank_backend": row.get("rerank_backend"),
            "rerank_selection_lane": row.get("rerank_selection_lane"),
            "rerank_selection_reason": row.get("rerank_selection_reason"),
            "pre_rerank_score": row.get("pre_rerank_score"),
            "rank_fusion_score": row.get("rank_fusion_score"),
            "rank_fusion_components": dict(row.get("rank_fusion_components") or {}),
            "pre_rank_fusion_score": row.get("pre_rank_fusion_score"),
            "authority_class": row.get("authority_class") or _authority_class(row),
            "authority_adjustment": row.get("authority_adjustment"),
            "authority_multiplier": row.get("authority_multiplier"),
            "authority_relevance_signal": row.get("authority_relevance_signal"),
            "authority_dual_backend_support": row.get("authority_dual_backend_support"),
            "authority_query_overlap": row.get("authority_query_overlap"),
            "authority_rerank_signal": row.get("authority_rerank_signal"),
            "authority_representation_reserved": bool(row.get("authority_representation_reserved")),
            "pre_authority_score": row.get("pre_authority_score"),
            "diversity_adjustment": row.get("diversity_adjustment"),
            "diversity_rank_reason": row.get("diversity_rank_reason"),
            "focus_composition_original_rank": row.get("focus_composition_original_rank"),
            "focus_composition_rank_adjustment": row.get("focus_composition_rank_adjustment"),
            "focus_composition_relevance_ratio": row.get("focus_composition_relevance_ratio"),
            "focus_composition_reason": row.get("focus_composition_reason"),
            "promotion_source_symbol_id": row.get("promotion_source_symbol_id"),
            "promotion_source_path": row.get("promotion_source_path"),
            "promotion_source_rank": row.get("promotion_source_rank"),
            "promotion_edge": row.get("promotion_edge"),
            "promotion_edge_ids": list(row.get("promotion_edge_ids") or []),
            "promotion_graph_distance": row.get("promotion_graph_distance"),
            "promotion_candidate_only": bool(row.get("promotion_candidate_only")),
            "promotion_query_overlap": row.get("promotion_query_overlap"),
            "refinement_candidate_only": bool(row.get("refinement_candidate_only")),
            "refinement_requalified": bool(row.get("refinement_requalified")),
            "refinement_parent_symbol_id": row.get("refinement_parent_symbol_id"),
            "refinement_parent_path": row.get("refinement_parent_path"),
            "refinement_parent_fused_rank": row.get("refinement_parent_fused_rank"),
            "refinement_parent_backends": list(row.get("refinement_parent_backends") or []),
            "refinement_depth": row.get("refinement_depth"),
            "refinement_enumeration": row.get("refinement_enumeration"),
            "refinement_reason": row.get("refinement_reason"),
            "refinement_query_overlap": row.get("refinement_query_overlap"),
            "retrieval_backends": backends,
            "match_reason": "; ".join(reason_by_backend.get(value, value) for value in backends),
            "raw_scores": dict(row.get("raw_scores") or {}),
            "resolution_status": row.get("resolution_status"),
            "resolution_method": row.get("resolution_method"),
            "candidate_count": row.get("candidate_count"),
            "confidence": row.get("confidence"),
            "line": row.get("line"),
            "call_source": row.get("call_source") or row.get("source_text"),
            "control_context": list(row.get("control_context") or []),
            "edge": row.get("edge"),
        }
        item["evidence_locator"] = EvidenceLocator(
            source_id=str(item.get("source_id") or ""),
            revision_key=str(item.get("revision_key") or ""),
            path=str(item.get("path") or ""),
            symbol=str(item.get("qualified_name") or item.get("symbol") or ""),
            start_line=int(item.get("start_line") or 0),
            end_line=int(item.get("end_line") or 0),
        ).as_dict()
        output.append(item)
        remaining = max(0, remaining - len(preview))
    return output


def _diagnostic_row(row: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    """Return one compact, evidence-identifiable retrieval diagnostic record.

    Diagnostic search is an observability surface, not a source-preview surface.
    Keep the ranking/refinement/reranker fields required to explain candidate
    movement while deliberately omitting source text, parser payloads, full
    score-component dictionaries, and other repeated metadata that previously
    caused MCP tool responses to truncate before global telemetry was serialized.
    """
    item: dict[str, Any] = {
        "rank": rank,
        "final_rank": row.get("final_rank"),
        "path": row.get("path"),
        "symbol": row.get("symbol_name") or row.get("name") or row.get("qualified_name"),
        "qualified_name": row.get("qualified_name"),
        "symbol_kind": row.get("symbol_kind") or row.get("kind"),
        "candidate_level": _candidate_level(row),
        "authority_class": row.get("authority_class") or _authority_class(row),
        "source_id": row.get("source_id") or row.get("repo_id"),
        "source_type": row.get("source_type") or ("git" if row.get("repo_id") else "directory"),
        "revision_key": row.get("revision_key") or row.get("branch_key"),
        "fused_rank": row.get("fused_rank"),
        "rerank_selection_lane": row.get("rerank_selection_lane"),
        "rerank_score": row.get("rerank_score"),
        "rerank_rank": row.get("rerank_rank"),
        "authority_adjustment": row.get("authority_adjustment"),
        "focus_composition_rank_adjustment": row.get("focus_composition_rank_adjustment"),
        "focus_composition_reason": row.get("focus_composition_reason"),
        "final_score": row.get("final_score"),
        "refinement_candidate_only": bool(row.get("refinement_candidate_only")),
        "refinement_requalified": bool(row.get("refinement_requalified")),
        "refinement_parent_path": row.get("refinement_parent_path"),
        "refinement_parent_fused_rank": row.get("refinement_parent_fused_rank"),
    }
    # Nulls dominate diagnostic JSON size while conveying no information. Keep
    # false booleans because they are meaningful negative observations.
    return {key: value for key, value in item.items() if value is not None}


def _diagnostic_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    candidates = rows[: max(1, min(limit, 50))]
    return [_diagnostic_row(row, rank=index) for index, row in enumerate(candidates, start=1)]


_DIAGNOSTIC_TRACE_COLUMNS = [
    "rank", "path", "symbol", "symbol_kind", "authority_class", "retrieval_backends",
    "fts_rank", "qdrant_rank", "fused_rank", "pre_rerank_rank", "refinement_state",
    "refinement_parent_path", "refinement_parent_fused_rank", "refinement_query_overlap",
    "rerank_composition_protected", "rerank_focus_lane_eligible", "rerank_focus_lane_signals", "rerank_focus_selection_order",
    "rerank_structural_lane_eligible", "rerank_structural_selection_order", "rerank_selection_lane",
    "rerank_refill_relevance_signals",
    "rerank_selection_exclusion", "rerank_selected", "rerank_score_returned", "rerank_score",
    "rerank_rank", "authority_adjustment", "focus_composition_rank_adjustment",
    "final_rank", "final_score",
]

_DIAGNOSTIC_SIGNAL_CODES = {
    "strong_refined_or_requalified_parent": "parent",
    "dual_fts_qdrant_support": "dual",
    "query_overlap": "overlap",
    "strong_fts_rank": "fts_rank",
    "strong_qdrant_rank": "qdrant_rank",
    "requested_test_role": "test_role",
    "requested_config_role": "config_role",
}

_DIAGNOSTIC_TRACE_LEGENDS = {
    "rerank_focus_lane_signals": {
        "parent": "strong refined/requalified parent",
        "dual": "dual FTS+Qdrant support",
        "overlap": "local query overlap met focus threshold",
        "fts_rank": "strong lexical backend rank",
        "qdrant_rank": "strong semantic backend rank",
        "test_role": "candidate matches explicit tests focus",
        "config_role": "candidate matches explicit config focus",
    },
    "rerank_refill_relevance_signals": {
        "parent": "strong refined/requalified parent",
        "dual": "corroborated dual FTS+Qdrant support",
        "overlap": "local query overlap met refill threshold",
        "fts_rank": "strong lexical backend rank",
        "qdrant_rank": "strong semantic backend rank",
    },
    "rerank_selection_exclusion": {
        "focus_budget": "focus lane eligible but higher-priority focus candidates consumed its budget",
        "structural_budget": "structural lane eligible but higher-priority structural candidates consumed its budget",
        "focus+structural_budget": "eligible for both reserved lanes but both budgets were consumed by higher-priority candidates",
        "general_cutoff": "outside general/refill cutoff and not eligible for a reserved lane",
    },
}


def _diagnostic_exclusion_code(row: dict[str, Any]) -> str:
    focus_eligible = bool(row.get("rerank_focus_lane_eligible"))
    structural_eligible = bool(row.get("rerank_structural_lane_eligible"))
    if row.get("rerank_selected"):
        return ""
    if focus_eligible and structural_eligible:
        return "focus+structural_budget"
    if focus_eligible:
        return "focus_budget"
    if structural_eligible:
        return "structural_budget"
    return "general_cutoff"


def _diagnostic_trace_row(row: dict[str, Any], rank: int) -> list[Any]:
    if row.get("refinement_requalified"):
        refinement_state = "requalified"
    elif row.get("refinement_candidate_only"):
        refinement_state = "generated"
    elif row.get("promotion_candidate_only"):
        refinement_state = "promoted"
    else:
        refinement_state = ""
    return [
        rank,
        row.get("path"),
        row.get("qualified_name") or row.get("symbol_name") or row.get("name"),
        row.get("symbol_kind") or row.get("kind"),
        row.get("authority_class") or _authority_class(row),
        [str(value) for value in (row.get("retrieval_backends") or [row.get("retrieval_backend")]) if value],
        row.get("fts_rank"),
        row.get("qdrant_rank"),
        row.get("fused_rank"),
        row.get("pre_rerank_rank"),
        refinement_state,
        row.get("refinement_parent_path"),
        row.get("refinement_parent_fused_rank"),
        row.get("refinement_query_overlap"),
        bool(row.get("rerank_composition_protected")),
        bool(row.get("rerank_focus_lane_eligible")),
        [_DIAGNOSTIC_SIGNAL_CODES.get(str(value), str(value)) for value in (row.get("rerank_focus_lane_signals") or [])],
        row.get("rerank_focus_selection_order"),
        bool(row.get("rerank_structural_lane_eligible")),
        row.get("rerank_structural_selection_order"),
        row.get("rerank_selection_lane"),
        [_DIAGNOSTIC_SIGNAL_CODES.get(str(value), str(value)) for value in (row.get("rerank_refill_relevance_signals") or [])],
        _diagnostic_exclusion_code(row),
        bool(row.get("rerank_selected")),
        bool(row.get("rerank_score_returned")),
        row.get("rerank_score"),
        row.get("rerank_rank"),
        row.get("authority_adjustment"),
        row.get("focus_composition_rank_adjustment"),
        row.get("final_rank"),
        row.get("final_score"),
    ]


def _diagnostic_candidate_trace(
    rows: list[dict[str, Any]],
    *,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a bounded page of the complete candidate trace in columnar form.

    R10.2 stores the full trace outside the primary MCP response. This helper is
    still the canonical encoder used both for persistence and paged retrieval.
    It intentionally contains no source text.
    """
    total = len(rows)
    start = max(0, int(offset or 0))
    stop = total if limit is None else min(total, start + max(0, int(limit)))
    trace_rows = [
        _diagnostic_trace_row(row, rank=index)
        for index, row in enumerate(rows[start:stop], start=start + 1)
    ]
    return {
        "encoding": "columns+rows",
        "pool_size": total,
        "offset": start,
        "returned": len(trace_rows),
        "has_more": stop < total,
        "columns": list(_DIAGNOSTIC_TRACE_COLUMNS),
        "legends": _DIAGNOSTIC_TRACE_LEGENDS,
        "rows": trace_rows,
    }


def _diagnostic_trace_descriptor(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe a stored full trace without inlining its rows."""
    return {
        "encoding": "stored-columns+rows",
        "pool_size": len(rows),
        "rows_inline": 0,
        "columns": list(_DIAGNOSTIC_TRACE_COLUMNS),
        "max_page_size": 50,
        "retrieval_tool": "code_diagnostics_trace",
    }


def _diagnostic_selected_candidates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Expose the finite reranker window compactly in the primary response."""
    columns = [
        "candidate_rank", "path", "symbol", "rerank_selection_lane",
        "focus_signals", "focus_order", "structural_order", "rerank_score",
        "rerank_rank", "final_rank",
    ]
    selected_rows: list[list[Any]] = []
    for index, row in enumerate(rows, start=1):
        if not row.get("rerank_selected"):
            continue
        selected_rows.append([
            index,
            row.get("path"),
            row.get("qualified_name") or row.get("symbol_name") or row.get("name"),
            row.get("rerank_selection_lane"),
            [_DIAGNOSTIC_SIGNAL_CODES.get(str(value), str(value)) for value in (row.get("rerank_focus_lane_signals") or [])],
            row.get("rerank_focus_selection_order"),
            row.get("rerank_structural_selection_order"),
            row.get("rerank_score"),
            row.get("rerank_rank"),
            row.get("final_rank"),
        ])
    return {
        "encoding": "columns+rows",
        "selected": len(selected_rows),
        "columns": columns,
        "rows": selected_rows,
    }



_DIAGNOSTIC_OWNER_NOISE = {
    "class", "func", "function", "method", "prototype", "receiver", "struct", "type",
}


def _diagnostic_target_terminal(target: str) -> tuple[str, str]:
    """Return ``(terminal_symbol, owner_prefix)`` without assuming a language.

    Covers common source/debug spellings such as Go receiver notation, Java/JS/
    Swift dotted members, C++/Ruby ``::``/``#`` forms, and Smali ``->`` members.
    It is intentionally lexical only; parser-native qualified names remain the
    authoritative symbol identity.
    """
    value = str(target or "").strip()
    if not value:
        return "", ""
    matches = list(re.finditer(r"(?:->|::|#|\.)([A-Za-z_$][A-Za-z0-9_$]*)", value))
    if matches:
        match = matches[-1]
        return match.group(1), value[:match.start()]
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value):
        return value, ""
    return "", ""


def _diagnostic_target_match(row: dict[str, Any], target: str) -> bool:
    needle = str(target or "").strip().casefold()
    if not needle:
        return False
    values = [
        str(row.get("path") or ""),
        str(row.get("qualified_name") or ""),
        str(row.get("symbol_name") or row.get("name") or ""),
    ]
    folded = [value.casefold() for value in values if value]
    if any(needle == value or needle in value for value in folded):
        return True

    # Parser-qualified names intentionally differ between languages. Compare a
    # target's terminal member plus lexical owner context rather than teaching
    # diagnostics Go/Java/JS/Swift/etc. syntax one by one.
    terminal, owner_prefix = _diagnostic_target_terminal(str(target or ""))
    row_symbol = str(row.get("symbol_name") or row.get("name") or "").strip()
    if not terminal or terminal.casefold() != row_symbol.casefold():
        return False
    if not owner_prefix:
        return True

    owner_identifiers = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", owner_prefix)
    target_owner = next(
        (value for value in reversed(owner_identifiers) if value.casefold() not in _DIAGNOSTIC_OWNER_NOISE),
        "",
    )
    target_owner_key = re.sub(r"[^a-z0-9]+", "", target_owner.casefold())
    if not target_owner_key:
        return True

    qname = str(row.get("qualified_name") or "")
    qname_prefix = qname
    if "->" in qname_prefix:
        qname_prefix = qname_prefix.rsplit("->", 1)[0]
    else:
        # Remove the terminal member from ordinary dotted/namespace-qualified
        # names before deriving the owner. Signatures after the member do not
        # participate in this lexical comparison.
        terminal_pos = qname_prefix.casefold().rfind(terminal.casefold())
        if terminal_pos >= 0:
            qname_prefix = qname_prefix[:terminal_pos].rstrip(".:#:/\\")
    row_owner_identifiers = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", qname_prefix)
    candidates = [row_owner_identifiers[-1]] if row_owner_identifiers else []
    path_stem = Path(str(row.get("path") or "")).stem
    if path_stem:
        candidates.append(path_stem)
    candidate_keys = {
        re.sub(r"[^a-z0-9]+", "", candidate.casefold())
        for candidate in candidates if candidate
    }
    return target_owner_key in candidate_keys


def _diagnostic_target_stage_records(
    targets: list[str] | None,
    stages: dict[str, list[dict[str, Any]]],
    *,
    max_targets: int = 16,
) -> dict[str, Any]:
    """Expose where named candidates first appear/disappear before reranking.

    This is intentionally compact: each stage reports presence/count/best rank
    plus one identifying record. It lets acceptance tests distinguish FTS,
    Qdrant, fusion, refinement, and composed-pool loss without inlining any
    source text or full candidate lists.
    """
    cleaned: list[str] = []
    for value in targets or []:
        target = str(value or "").strip()
        if target and target not in cleaned:
            cleaned.append(target)
        if len(cleaned) >= max_targets:
            break

    items: list[dict[str, Any]] = []
    for target in cleaned:
        stage_data: dict[str, Any] = {}
        for stage_name, rows in stages.items():
            matches = [
                (index, row)
                for index, row in enumerate(rows, start=1)
                if _diagnostic_target_match(row, target)
            ]
            best = matches[0] if matches else None
            stage_data[stage_name] = {
                "found": bool(matches),
                "match_count": len(matches),
                "best_rank": best[0] if best else None,
                "path": best[1].get("path") if best else None,
                "symbol": (best[1].get("qualified_name") or best[1].get("symbol_name") or best[1].get("name")) if best else None,
                "refinement_requalified": bool(best[1].get("refinement_requalified")) if best else False,
                "rerank_composition_protected": bool(best[1].get("rerank_composition_protected")) if best else False,
                "lexical_match_mode": best[1].get("lexical_match_mode") if best else None,
                "lexical_normalization_terms": list(best[1].get("lexical_normalization_terms") or []) if best else [],
            }
        items.append({"target": target, "stages": stage_data})
    return {
        "requested": len(targets or []),
        "applied": len(cleaned),
        "items": items,
    }

def _diagnostic_target_records(
    rows: list[dict[str, Any]],
    targets: list[str] | None,
    *,
    max_targets: int = 16,
    max_matches_per_target: int = 5,
) -> dict[str, Any]:
    """Return complete trace records for explicitly named diagnostic targets."""
    cleaned: list[str] = []
    for value in targets or []:
        target = str(value or "").strip()
        if target and target not in cleaned:
            cleaned.append(target)
        if len(cleaned) >= max_targets:
            break

    output: list[dict[str, Any]] = []
    for target in cleaned:
        needle = target.casefold()
        exact: list[tuple[int, dict[str, Any]]] = []
        partial: list[tuple[int, dict[str, Any]]] = []
        for index, row in enumerate(rows, start=1):
            values = [
                str(row.get("path") or ""),
                str(row.get("qualified_name") or ""),
                str(row.get("symbol_name") or row.get("name") or ""),
            ]
            folded = [value.casefold() for value in values if value]
            if any(needle == value for value in folded):
                exact.append((index, row))
            elif _diagnostic_target_match(row, target):
                partial.append((index, row))
        matches = exact + partial
        records = []
        for index, row in matches[:max_matches_per_target]:
            encoded = _diagnostic_trace_row(row, index)
            records.append(dict(zip(_DIAGNOSTIC_TRACE_COLUMNS, encoded)))
        output.append({
            "target": target,
            "found": bool(matches),
            "match_count": len(matches),
            "matches_truncated": len(matches) > max_matches_per_target,
            "matches": records,
        })
    return {
        "requested": len(targets or []),
        "applied": len(cleaned),
        "max_targets": max_targets,
        "items": output,
    }

def _compact_retrieval_diagnostics(retrieval: dict[str, Any]) -> dict[str, Any]:
    """Canonical non-duplicative retrieval telemetry for ``view=diagnostics``."""
    reranker = dict(retrieval.get("reranker") or {})
    selection = dict(reranker.get("selection") or retrieval.get("rerank_selection") or {})
    compact_reranker = {
        "requested": retrieval.get("rerank_requested", reranker.get("requested")),
        "eligible": retrieval.get("rerank_eligible"),
        "attempted": retrieval.get("rerank_attempted", reranker.get("attempted")),
        "applied": retrieval.get("rerank_applied", reranker.get("applied")),
        "backend": retrieval.get("rerank_backend", reranker.get("backend")),
        "model": reranker.get("model"),
        "latency_ms": retrieval.get("rerank_latency_ms", reranker.get("latency_ms")),
        "timeout_seconds": reranker.get("timeout_seconds"),
        "timeout_source": reranker.get("timeout_source"),
        "reason": retrieval.get("rerank_reason", reranker.get("reason")),
        "failure_class": reranker.get("failure_class"),
        "retryable": reranker.get("retryable"),
        "degraded": reranker.get("degraded"),
        "candidates_in": retrieval.get("rerank_candidates_in", reranker.get("candidates_in")),
        "candidates_selected": retrieval.get("rerank_candidates_selected", reranker.get("candidates_selected")),
        "candidates_not_selected": retrieval.get("rerank_candidates_not_selected", reranker.get("candidates_not_selected")),
        "request_documents": retrieval.get("rerank_request_documents", reranker.get("request_documents")),
        "configured_top_n": reranker.get("configured_top_n"),
        "results_requested_top_n": retrieval.get("rerank_results_requested_top_n", reranker.get("results_requested_top_n")),
        "scores_returned_to_awoki": retrieval.get("rerank_scores_returned_to_awoki", reranker.get("scores_returned_to_awoki")),
        "selected_without_returned_score": retrieval.get(
            "rerank_selected_without_returned_score", reranker.get("selected_without_returned_score")
        ),
        "backend_scoring_coverage": retrieval.get("rerank_backend_scoring_coverage", reranker.get("backend_scoring_coverage")),
        "post_rerank_pool_size": retrieval.get("post_rerank_pool_size", reranker.get("post_rerank_pool_size")),
        "selection": selection,
    }
    compact = {
        "fts": {
            "requested": retrieval.get("fts_requested"),
            "used": retrieval.get("fts_used"),
            "candidates": retrieval.get("fts_candidates"),
        },
        "qdrant": {
            "requested": retrieval.get("qdrant_requested"),
            "eligible": retrieval.get("qdrant_eligible"),
            "used": retrieval.get("qdrant_used"),
            "candidates": retrieval.get("qdrant_candidates"),
        },
        "embedding": {
            "attempted": retrieval.get("embedding_attempted"),
            "status": retrieval.get("embedding_status"),
            "latency_ms": retrieval.get("embedding_latency_ms"),
        },
        "reranker": compact_reranker,
        "structural_promotion": {
            "requested": retrieval.get("structural_promotion_requested"),
            "generated": retrieval.get("structural_promotions"),
        },
        "symbol_refinement": {
            "requested": retrieval.get("symbol_refinement_requested"),
            "generated": retrieval.get("symbol_refinements"),
            "limits": retrieval.get("symbol_refinement_limits") or {},
            "diagnostics": retrieval.get("symbol_refinement_diagnostics") or {},
        },
        "result_focus": retrieval.get("result_focus"),
        "result_focus_reason": retrieval.get("result_focus_reason"),
        "focus_composition": retrieval.get("focus_composition") or {},
        "stage_top": retrieval.get("stage_top") or {},
    }
    return compact


def _diagnostic_details(
    details: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    diagnostic_targets: list[str] | None = None,
) -> dict[str, Any]:
    """Produce diagnostics-first output while keeping the full trace out-of-band."""
    output: dict[str, Any] = {}
    if "retrieval" in details:
        output["retrieval"] = _compact_retrieval_diagnostics(dict(details.get("retrieval") or {}))
        if "diagnostic_target_stages" in details:
            output["diagnostic_target_stages"] = details["diagnostic_target_stages"]
        output["candidate_trace"] = _diagnostic_trace_descriptor(candidate_rows)
        # Exact acceptance/debug targets are more important than the convenience
        # selected-window summary, so serialize them first under transport pressure.
        if diagnostic_targets:
            output["diagnostic_targets"] = _diagnostic_target_records(candidate_rows, diagnostic_targets)
        output["rerank_selected_candidates"] = _diagnostic_selected_candidates(candidate_rows)
    for key in ("candidates", "vector_search", "symbol", "exact_term", "ambiguity", "source", "target", "source_candidates", "target_candidates"):
        if key in details:
            output[key] = details[key]
    return output

def _definition_rows(db: Path, project_id: str, branch_key: str, query: str, limit: int) -> tuple[str, list[dict[str, Any]]]:
    symbol = _extract_symbol_candidate(query)
    rows = store.definitions(db, project_id, branch_key, symbol, limit=max(limit, 20))
    return symbol, rows


def search_project_code(
    paths: Any,
    project_id: str,
    query: str,
    *,
    mode: str = "auto",
    view: str = "context",
    limit: int = 10,
    refresh_index: bool = False,
    include_qdrant: bool = True,
    use_fts: bool = True,
    use_qdrant: bool = True,
    use_reranker: bool = True,
    result_focus: str = "auto",
    structural_promotion: bool = True,
    strict_backends: bool = False,
    max_chars: int = 0,
    ensure_index: bool = True,
    repo: str = "",
    source: str = "",
    diagnostic_targets: list[str] | None = None,
) -> dict[str, Any]:
    if not query.strip():
        return {"status": "rejected", "reason": "query cannot be empty", "project_id": project_id}
    view = (view or "context").lower()
    if view not in {"peek", "context", "full", "diagnostics"}:
        view = "context"
    route = route_query(query, mode)
    selected = route["mode"]
    if selected == "invalid":
        return {
            "status": "rejected",
            "project_id": project_id,
            "query": query,
            "reason": route["reason"],
            "routing": {"selected_mode": "invalid", "explicit_mode": mode},
        }
    focus_info = _result_focus(query, result_focus)
    if focus_info["focus"] == "invalid":
        return {
            "status": "rejected",
            "project_id": project_id,
            "query": query,
            "reason": focus_info["reason"],
        }
    focus = focus_info["focus"]
    semantic_mode = selected in {"conceptual", "similar"}
    existing_status: dict[str, Any] = {}
    readiness = _search_index_readiness(paths, project_id, repo=repo, source=source)
    if ensure_index:
        if refresh_index or not readiness.get("lexical_current"):
            # Interactive search may establish/refresh the local structural
            # SQLite index, but it never performs remote Qdrant synchronization
            # or repository embedding work implicitly. Semantic vectors are an
            # optional acceleration layer and are refreshed explicitly through
            # project_refresh(include_code=true, include_qdrant=true).
            index = ensure_current(
                paths, project_id, include_qdrant=False, force=refresh_index, repo=repo, source=source
            )
            if index.get("status") in {"not_found", "rejected", "stale_source", "invalid_repo_root"}:
                return index
            readiness = _search_index_readiness(paths, project_id, repo=repo, source=source)
        else:
            index = {
                "status": "existing",
                "project_id": project_id,
                "freshness": readiness,
            }
    else:
        # Cross-project and other explicit no-refresh callers require a proven
        # freshness decision even for non-Git/dirty fixtures, so retain the
        # full deterministic status check on that less latency-sensitive path.
        existing_status = index_status(
            paths, project_id, deep_verify=True, verify_qdrant=False, repo=repo, source=source
        )
        freshness = existing_status.get("freshness") or {}
        if existing_status.get("status") in {"not_found", "not_indexed", "stale"}:
            return {
                "status": existing_status.get("status"),
                "project_id": project_id,
                "query": query,
                "reason": "existing active-branch code index is unavailable or stale; set refresh_stale=true to rebuild it",
                "routing": route,
                "index_status": existing_status,
            }
        readiness = {
            **readiness,
            "lexical_current": bool(freshness.get("lexical_current")),
            "vector_current": bool(freshness.get("vector_current")),
        }
        index = {
            "status": "existing",
            "project_id": project_id,
            "freshness": freshness,
        }
    _, repo_root, branch, db, resolved = _project_context(paths, project_id, repo=repo, source=source)
    state = store.read_state(db, branch.branch_key)
    vector_current_status = bool(readiness.get("vector_current"))
    effective_qdrant = bool(include_qdrant and use_qdrant and semantic_mode and selected != "lexical")
    effective_reranker = bool(use_reranker and semantic_mode and selected != "lexical")
    vector_available = bool(effective_qdrant and vector_current_status)
    if strict_backends and effective_qdrant and not vector_current_status:
        return {
            "status": "backend_unavailable",
            "project_id": project_id,
            "query": query,
            "backend": "qdrant",
            "reason": str(readiness.get("vector_reason") or "current code vectors are unavailable for this active branch"),
            "routing": {"selected_mode": selected, "explicit_mode": mode},
        }
    if strict_backends and effective_reranker and not rag_backend.rerank_enabled():
        return {
            "status": "backend_unavailable",
            "project_id": project_id,
            "query": query,
            "backend": "reranker",
            "reason": "reranker was explicitly requested but is not configured/enabled",
            "routing": {"selected_mode": selected, "explicit_mode": mode},
        }
    candidate_limit = max(30, min(limit * 5, 150))
    rows: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    if selected == "definition":
        symbol, rows = _definition_rows(db, project_id, branch.branch_key, query, candidate_limit)
        details["symbol"] = symbol
        if len(rows) > 1:
            details["ambiguity"] = len(rows)
    elif selected == "exact":
        exact = _extract_symbol_candidate(query) if not IDENTIFIER_RE.match(query.strip()) else query.strip()
        rows = store.exact_search(db, project_id, branch.branch_key, exact, candidate_limit)
        details["exact_term"] = exact
    elif selected == "callers":
        symbol, definitions = _definition_rows(db, project_id, branch.branch_key, query, 20)
        details["symbol"] = symbol
        if len(definitions) == 1:
            rows = store.callers(db, branch.branch_key, str(definitions[0]["symbol_id"]), str(definitions[0]["symbol_name"]))
        else:
            details["ambiguity"] = len(definitions)
            rows = definitions
    elif selected == "callees":
        symbol, definitions = _definition_rows(db, project_id, branch.branch_key, query, 20)
        details["symbol"] = symbol
        if len(definitions) == 1:
            rows = store.callees(db, branch.branch_key, str(definitions[0]["symbol_id"]))
        else:
            details["ambiguity"] = len(definitions)
            rows = definitions
    elif selected == "path":
        endpoints = _path_endpoints(query)
        if endpoints is None:
            return {
                "status": "inconclusive",
                "project_id": project_id,
                "query": query,
                "reason": "could not deterministically extract source and target symbols",
                "routing": route,
                "index": index,
            }
        source_name, target_name = endpoints
        source_defs = store.definitions(db, project_id, branch.branch_key, source_name, limit=20)
        target_defs = store.definitions(db, project_id, branch.branch_key, target_name, limit=20)
        details.update({"source": source_name, "target": target_name, "source_candidates": len(source_defs), "target_candidates": len(target_defs)})
        if not source_defs or not target_defs:
            return {
                "status": "not_found",
                "project_id": project_id,
                "query": query,
                "routing": route,
                "details": details,
                "index": index,
            }
        graph = store.graph_path(
            db,
            branch.branch_key,
            [str(row["symbol_id"]) for row in source_defs],
            {str(row["symbol_id"]) for row in target_defs},
        )
        trace_rows: list[dict[str, Any]] = []
        if graph.get("status") == "found":
            first = store.symbol_by_id(db, str(graph["path"][0]["source_symbol_id"])) if graph.get("path") else source_defs[0]
            if first:
                first["score"] = 2.0
                first["retrieval_backend"] = "call_graph_path"
                trace_rows.append(first)
            for edge in graph.get("path", []):
                target = store.symbol_by_id(db, str(edge.get("target_symbol_id") or ""))
                if target:
                    target.update({"score": 2.0, "retrieval_backend": "call_graph_path", "edge": edge})
                    trace_rows.append(target)
        rows = trace_rows
        details["graph"] = graph
    else:
        fts = store.search_fts(db, project_id, branch.branch_key, query, candidate_limit) if use_fts else []
        exact = [] if selected == "lexical" else store.exact_search(db, project_id, branch.branch_key, query, candidate_limit)
        vector_started = time.monotonic()
        if vector_available:
            vector_search = vector_store.search_with_status(
                query, project_id=project_id, branch_key=branch.branch_key, limit=candidate_limit
            )
        else:
            if not include_qdrant or not use_qdrant:
                vector_skip_reason = "disabled_by_query_controls"
            elif not semantic_mode or selected == "lexical":
                vector_skip_reason = "not_requested_for_selected_mode"
            else:
                vector_skip_reason = "current code vectors are unavailable for this active branch"
            vector_search = {
                "status": "skipped",
                "backend": "qdrant",
                "collection": vector_store.code_collection_name(),
                "reason": vector_skip_reason,
                "hits": [],
            }
        vector_latency_ms = int((time.monotonic() - vector_started) * 1000) if vector_available else 0
        if strict_backends and effective_qdrant and vector_search.get("status") != "ok":
            return {
                "status": "backend_unavailable",
                "project_id": project_id,
                "query": query,
                "backend": "qdrant",
                "reason": str(vector_search.get("reason") or "Qdrant query did not complete successfully"),
                "routing": {"selected_mode": selected, "explicit_mode": mode},
            }
        vector = _hydrate_vector_rows(db, list(vector_search.get("hits") or []))
        if selected == "similar":
            symbol, definitions = _definition_rows(db, project_id, branch.branch_key, query, 5)
            if definitions:
                source_text = str(definitions[0].get("text") or definitions[0].get("signature") or symbol)
                fts = store.search_fts(db, project_id, branch.branch_key, source_text[:2000], candidate_limit)
                vector_search = (
                    vector_store.search_with_status(
                        source_text[:4000],
                        project_id=project_id,
                        branch_key=branch.branch_key,
                        limit=candidate_limit,
                    )
                    if vector_available
                    else vector_search
                )
                if strict_backends and effective_qdrant and vector_search.get("status") != "ok":
                    return {
                        "status": "backend_unavailable",
                        "project_id": project_id,
                        "query": query,
                        "backend": "qdrant",
                        "reason": str(vector_search.get("reason") or "Qdrant query did not complete successfully"),
                        "routing": {"selected_mode": selected, "explicit_mode": mode},
                    }
                vector = _hydrate_vector_rows(db, list(vector_search.get("hits") or []))
                details["source_symbol"] = symbol
        hit_lists: list[list[dict[str, Any]]] = []
        if exact:
            hit_lists.append(exact)
        if fts:
            hit_lists.append(fts)
        if vector:
            hit_lists.append(vector)
        rows = _merge_ranked(query, hit_lists, candidate_limit) if hit_lists else []
        promotions: list[dict[str, Any]] = []
        refinements: list[dict[str, Any]] = []
        refinement_diagnostics: dict[str, Any] = {}
        focus_composition_diagnostics: dict[str, Any] = {}
        post_rerank_rows: list[dict[str, Any]] = []
        raw_fused_rows = [dict(row) for row in rows]
        post_refinement_discovery_rows = [dict(row) for row in rows]
        composed_pre_rerank_rows = [dict(row) for row in rows]
        rerank_telemetry = {
            "requested": bool(effective_reranker),
            "configured": bool(rag_backend.rerank_enabled()),
            "attempted": False,
            "applied": False,
            "backend": str(rag_backend.rerank_profile().get("provider") or ""),
            "candidates_in": 0,
            "results_out": 0,
            "latency_ms": 0,
            "reason": "not a semantic search" if not semantic_mode else "",
        }
        if selected in {"conceptual", "similar"}:
            if structural_promotion and focus != "tests":
                promotions = _structural_promotions(db, branch.branch_key, rows, query)
            if focus == "implementation":
                refinements = _symbol_refinements(
                    db,
                    rows,
                    query,
                    focus=focus,
                    diagnostics=refinement_diagnostics,
                )
            post_refinement_discovery_rows = [dict(row) for row in rows]
            if promotions or refinements:
                rerank_eval_limit = (
                    int(rag_backend.rerank_profile().get("candidate_limit") or candidate_limit)
                    if effective_reranker
                    else candidate_limit
                )
                rows = _compose_rerank_candidates(
                    rows,
                    promotions,
                    candidate_limit,
                    refinements=refinements,
                    evaluation_limit=rerank_eval_limit,
                )
            composed_pre_rerank_rows = [dict(row) for row in rows]
            for candidate_rank, candidate in enumerate(rows, start=1):
                candidate["pre_rerank_rank"] = candidate_rank
            rows = _annotate_authority(rows)
            rows, rerank_telemetry = _rerank(
                query,
                rows,
                candidate_limit,
                enabled=effective_reranker,
                focus=focus,
            )
            post_rerank_rows = [dict(row) for row in rows]
            if strict_backends and effective_reranker and (
                not rerank_telemetry.get("attempted") or not rerank_telemetry.get("applied")
            ):
                return {
                    "status": "backend_unavailable",
                    "project_id": project_id,
                    "query": query,
                    "backend": "reranker",
                    "reason": str(rerank_telemetry.get("reason") or "reranker did not produce explicit scores"),
                    "routing": {"selected_mode": selected, "explicit_mode": mode},
                    "details": {"retrieval": {"reranker": rerank_telemetry}},
                }
            rows = _apply_authority_prior(query, rows, focus)
            rows = _diversify_results(rows, focus)
            rows = _compose_focus_results(rows, focus, diagnostics=focus_composition_diagnostics)
        else:
            rows = _annotate_authority(rows)
            for final_rank, row in enumerate(rows, start=1):
                row["final_rank"] = final_rank
                row["final_score"] = float(row.get("score") or 0.0)
        details["candidates"] = {
            "exact": len(exact),
            "fts": len(fts),
            "qdrant": len(vector),
            "structural_promotions": len(promotions),
            "symbol_refinements": len(refinements),
        }
        details["vector_search"] = {
            "status": vector_search.get("status"),
            "collection": vector_search.get("collection"),
            "reason": vector_search.get("reason"),
            "latency_ms": vector_latency_ms,
        }
        if view == "diagnostics" and diagnostic_targets:
            details["diagnostic_target_stages"] = _diagnostic_target_stage_records(
                diagnostic_targets,
                {
                    "fts": [dict(row) for row in fts],
                    "qdrant": [dict(row) for row in vector],
                    "fused": raw_fused_rows,
                    "post_refinement_discovery": post_refinement_discovery_rows,
                    "composed_pool": composed_pre_rerank_rows,
                },
            )
        final_stage_rows = [dict(row) for row in rows]

        def stage_preview(stage_rows: list[dict[str, Any]], score_field: str) -> list[dict[str, Any]]:
            preview: list[dict[str, Any]] = []
            for rank, row in enumerate(stage_rows[: min(10, max(1, limit))], start=1):
                preview.append({
                    "rank": rank,
                    "path": row.get("path"),
                    "symbol": row.get("symbol_name") or row.get("qualified_name"),
                    "symbol_kind": row.get("symbol_kind") or row.get("kind"),
                    "candidate_level": _candidate_level(row),
                    "authority_class": row.get("authority_class") or _authority_class(row),
                    "score": row.get(score_field) if row.get(score_field) is not None else row.get("score"),
                    "backends": list(row.get("retrieval_backends") or ([row.get("retrieval_backend")] if row.get("retrieval_backend") else [])),
                    "promotion_candidate_only": bool(row.get("promotion_candidate_only")),
                    "refinement_candidate_only": bool(row.get("refinement_candidate_only")),
                    "refinement_requalified": bool(row.get("refinement_requalified")),
                    "rerank_selection_lane": row.get("rerank_selection_lane"),
                    "rerank_rank": row.get("rerank_rank"),
                })
            return preview

        details["retrieval"] = {
            "fts_requested": bool(use_fts),
            "fts_used": bool(use_fts and selected in {"lexical", "conceptual", "similar"}),
            "fts_candidates": len(fts),
            "qdrant_requested": bool(use_qdrant),
            "qdrant_eligible": bool(semantic_mode and selected != "lexical"),
            "qdrant_used": bool(vector_available and vector_search.get("status") == "ok"),
            "qdrant_candidates": len(vector),
            "embedding_attempted": bool(vector_available),
            "embedding_status": "ok" if vector_available and vector_search.get("status") == "ok" else ("skipped" if not vector_available else "degraded"),
            "embedding_latency_ms": vector_latency_ms,
            "structural_promotion_requested": bool(structural_promotion and semantic_mode and focus != "tests"),
            "structural_promotions": len(promotions),
            "symbol_refinement_requested": bool(semantic_mode and focus == "implementation"),
            "symbol_refinements": len(refinements),
            "symbol_refinement_diagnostics": refinement_diagnostics,
            "symbol_refinement_limits": {
                "max_parents": 8,
                "max_children_per_parent": 8,
                "max_total": 24,
                "max_depth": 2,
            },
            "reranker": rerank_telemetry,
            "rerank_requested": bool(use_reranker),
            "rerank_eligible": bool(semantic_mode and selected != "lexical"),
            "rerank_attempted": bool(rerank_telemetry.get("attempted")),
            "rerank_applied": bool(rerank_telemetry.get("applied")),
            "rerank_backend": rerank_telemetry.get("backend"),
            "rerank_timeout_seconds": rerank_telemetry.get("timeout_seconds"),
            "rerank_timeout_source": rerank_telemetry.get("timeout_source"),
            "rerank_failure_class": rerank_telemetry.get("failure_class"),
            "rerank_retryable": rerank_telemetry.get("retryable"),
            "rerank_degraded": rerank_telemetry.get("degraded"),
            "rerank_candidates_in": rerank_telemetry.get("candidates_in"),
            "rerank_results_out": rerank_telemetry.get("results_out"),
            "rerank_candidates_selected": rerank_telemetry.get("candidates_selected"),
            "rerank_request_documents": rerank_telemetry.get("request_documents"),
            "rerank_scores_returned": rerank_telemetry.get("scores_returned"),
            "rerank_scores_returned_to_awoki": rerank_telemetry.get("scores_returned_to_awoki"),
            "rerank_results_requested_top_n": rerank_telemetry.get("results_requested_top_n"),
            "rerank_candidates_scored": rerank_telemetry.get("candidates_scored"),
            "rerank_candidates_unscored": rerank_telemetry.get("candidates_unscored"),
            "rerank_selected_without_returned_score": rerank_telemetry.get("selected_without_returned_score"),
            "rerank_backend_scoring_coverage": rerank_telemetry.get("backend_scoring_coverage"),
            "rerank_selection": dict(rerank_telemetry.get("selection") or {}),
            "rerank_candidates_not_selected": rerank_telemetry.get("candidates_not_selected"),
            "post_rerank_pool_size": rerank_telemetry.get("post_rerank_pool_size"),
            "rerank_latency_ms": rerank_telemetry.get("latency_ms"),
            "rerank_reason": rerank_telemetry.get("reason"),
            "result_focus": focus,
            "result_focus_reason": focus_info["reason"],
            "focus_composition": focus_composition_diagnostics,
            "stage_top": {
                "fused": stage_preview(raw_fused_rows, "fused_score"),
                "promoted": stage_preview(promotions, "score"),
                "refined": stage_preview(refinements, "score"),
                "reranked": stage_preview(post_rerank_rows, "rank_fusion_score"),
                "final": stage_preview(final_stage_rows, "final_score"),
            },
        }

    hits = _bounded_rows(db, rows, view, limit, max_chars=max_chars)
    response_details = _diagnostic_details(details, rows, diagnostic_targets) if view == "diagnostics" else details
    ambiguous = selected in {"definition", "callers", "callees"} and len(rows) > 1 and details.get("ambiguity")
    lowered_query = query.lower()
    flow_oriented = bool(re.search(
        r"\b(flow|trace|execution|process(?:ing|ed)?|decision|branch|input|lifecycle|pipeline|path)\b",
        lowered_query,
    ))
    indexed_repository_evidence = readiness.get("indexed_repository_evidence") or {}
    semantics_operations = _recommended_go_semantics_operations(query)
    followups = (
        ["code_definition", "code_flow_graph", "code_source_window", "code_validate_claim"]
        if flow_oriented
        else ["code_definition", "code_source_window", "code_validate_claim"]
    )
    if semantics_operations:
        followups.append("code_semantics_check")
    response = {
        "status": "ambiguous" if ambiguous else "ok",
        "project_id": project_id,
        "query": query,
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "scope": {
            "project_id": project_id,
            "repo_id": branch.repo_id,
            "source_id": branch.source_id,
            "source_type": branch.source_type,
            "revision_key": branch.revision_key,
            "revision_label": branch.revision_label,
            "content_identity": branch.content_identity,
            "branch_key": branch.branch_key,
            "branch_name": branch.branch_name,
            "commit_sha": branch.commit_sha,
            "dirty": branch.dirty,
            "repository_assurance": readiness.get(
                "repository_assurance",
                "CONTENT_MANIFEST_BOUND" if branch.source_type != "git" else "WORKING_TREE_BOUND",
            ),
            "tree_sha": indexed_repository_evidence.get("raw_tree_sha", ""),
        },
        "routing": {
            "selected_mode": selected,
            "reason": route["reason"],
            "explicit_mode": mode,
            "result_focus": focus,
            "result_focus_reason": focus_info["reason"],
        },
        "view": view,
    }
    # Diagnostics are serialized before final-top-K hits so transport truncation
    # cannot erase the counters/stage summaries that explain the retrieval run.
    # Normal views retain the historical hits-before-details response order.
    if view == "diagnostics":
        response["details"] = response_details
        response["hits"] = hits
        # Internal handoff only: harness_core persists this metadata-only trace
        # and removes it before the MCP response crosses the transport boundary.
        response["_diagnostic_trace"] = _diagnostic_candidate_trace(rows)
    else:
        response["hits"] = hits
        response["details"] = response_details
    response.update({
        "index": index,
        "freshness": {
            "source_probe_hash": state.get("source_probe_hash"),
            "document_set_hash": state.get("document_set_hash"),
            "lexical_current": bool(state),
            "vector_current": vector_current_status,
            "vector_reason": state.get("vector_reason"),
            "vector_query_status": (details.get("vector_search") or {}).get("status"),
            "vector_query_reason": (details.get("vector_search") or {}).get("reason"),
        },
        "analysis_policy": {
            "evidence_backed_default": True,
            "flow_oriented": flow_oriented,
            "semantic_is_discovery_only": True,
            "recommended_followup_tools": followups,
            "deterministic_semantics_check_recommended": "code_semantics_check" in followups,
            "recommended_semantics_operations": semantics_operations,
            "strict_claim_validation": "selective_atomic_proof",
        },
        "rules": [
            "Results are restricted to the selected registered evidence source and revision.",
            "Semantic/FTS results may discover candidates but are not sufficient evidence for behavioral assertions.",
            "Inspect bounded, current authoritative source and structural relationships before asserting implementation behavior.",
            "For flow questions, build a bounded relevant structural graph and inspect control/data details in exact source rather than dumping the whole repository graph.",
            "Call-graph paths are static possibilities; ambiguous and dynamic edges are never guessed.",
            "When a claim depends on a supported Go language/standard-library primitive, prefer code_semantics_check over model arithmetic or remembered runtime semantics.",
        ],
    })
    return response


def definition_lookup(paths: Any, project_id: str, symbol: str, *, view: str = "context", limit: int = 10, refresh_index: bool = False, repo: str = "", source: str = "") -> dict[str, Any]:
    return search_project_code(
        paths, project_id, f"Where is {symbol} defined?", mode="definition", view=view,
        limit=limit, refresh_index=refresh_index, include_qdrant=False, repo=repo, source=source,
    )


def callers_lookup(paths: Any, project_id: str, symbol: str, *, view: str = "context", limit: int = 20, refresh_index: bool = False, repo: str = "", source: str = "") -> dict[str, Any]:
    return search_project_code(
        paths, project_id, f"Who calls {symbol}?", mode="callers", view=view,
        limit=limit, refresh_index=refresh_index, include_qdrant=False, repo=repo, source=source,
    )


def callees_lookup(paths: Any, project_id: str, symbol: str, *, view: str = "context", limit: int = 20, refresh_index: bool = False, repo: str = "", source: str = "") -> dict[str, Any]:
    return search_project_code(
        paths, project_id, f"What does {symbol} call?", mode="callees", view=view,
        limit=limit, refresh_index=refresh_index, include_qdrant=False, repo=repo, source=source,
    )


def path_lookup(paths: Any, project_id: str, source: str, target: str, *, view: str = "context", limit: int = 30, refresh_index: bool = False, repo: str = "", source_id: str = "") -> dict[str, Any]:
    return search_project_code(
        paths, project_id, f"{source} -> {target}", mode="path", view=view,
        limit=limit, refresh_index=refresh_index, include_qdrant=False, repo=repo, source=source_id,
    )


def flow_graph_lookup(
    paths: Any,
    project_id: str,
    symbol: str,
    *,
    max_depth: int = 5,
    max_nodes: int = 120,
    max_edges: int = 400,
    refresh_index: bool = False,
    repo: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Return a bounded structural flow graph rooted at one exact symbol.

    This is an investigation primitive, not a runtime trace and not a replacement
    for strict atomic claim validation. It deliberately traverses only resolved
    call edges while retaining unresolved/ambiguous boundaries for the caller.
    """
    index = ensure_current(
        paths, project_id, include_qdrant=False, force=refresh_index, repo=repo, source=source
    )
    if index.get("status") not in {"indexed", "current"}:
        return index
    _, repo_root, branch, db, resolved = _project_context(paths, project_id, repo=repo, source=source)
    definitions = store.definitions(db, project_id, branch.branch_key, symbol, limit=20)
    if len(definitions) != 1:
        return {
            "status": "ambiguous" if definitions else "not_found",
            "project_id": project_id,
            "symbol": symbol,
            "candidate_count": len(definitions),
            "candidates": _bounded_rows(db, definitions, "peek", 20),
            "reason": "flow graph requires one exact root symbol; resolve ambiguity before traversal",
            "index": index,
        }
    root = definitions[0]
    graph = store.reachable_graph(
        db,
        branch.branch_key,
        [str(root["symbol_id"])],
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "scope": {
            "project_id": project_id,
            "repo_id": branch.repo_id,
            "source_id": branch.source_id,
            "source_type": branch.source_type,
            "revision_key": branch.revision_key,
            "revision_label": branch.revision_label,
            "content_identity": branch.content_identity,
            "branch_key": branch.branch_key,
            "branch_name": branch.branch_name,
            "commit_sha": branch.commit_sha,
            "dirty": branch.dirty,
        },
        "root": {
            "symbol_id": root.get("symbol_id"),
            "symbol": root.get("symbol_name") or root.get("name"),
            "qualified_name": root.get("qualified_name"),
            "path": root.get("path"),
            "start_line": root.get("start_line"),
            "end_line": root.get("end_line"),
        },
        "graph": graph,
        "index": index,
        "rules": [
            "Use this graph to scope investigation, then inspect exact source/AST for branch conditions, assignments, arguments, and outcomes.",
            "Only resolved calls are traversed; ambiguous and unresolved calls are evidence boundaries, not guessed edges.",
            "Use code_validate_claim selectively for bounded propositions that require a strict VERIFIED/REFUTED verdict.",
        ],
    }


def source_window(
    paths: Any,
    project_id: str,
    rel_path: str,
    *,
    start_line: int = 1,
    end_line: int = 0,
    max_chars: int = 20000,
    max_line_chars: int = 4096,
    refresh_index: bool = False,
    repo: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Read a bounded, hash-checked source window from the active indexed branch.

    The function never returns an arbitrary complete giant source line. Each line
    and the total response are bounded, while the full indexed file hash is still
    checked before the window is returned.
    """
    if refresh_index:
        refreshed = ensure_current(
            paths, project_id, include_qdrant=False, force=True, repo=repo, source=source
        )
        if refreshed.get("status") not in {"indexed", "current"}:
            return refreshed
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, db, resolved = context
    if not db.exists():
        return {
            "status": "not_indexed",
            "project_id": project_id,
            "reason": "structural code index does not exist; run codebase_search or refresh code first",
        }
    raw_rel = str(rel_path or "").strip().replace("\\", "/")
    candidate = Path(raw_rel)
    if not raw_rel or candidate.is_absolute() or ".." in candidate.parts:
        return {"status": "rejected", "project_id": project_id, "reason": "path must be a safe repository-relative path"}
    record = store.file_record(db, project_id, branch.branch_key, candidate.as_posix())
    if not record:
        return {
            "status": "not_found",
            "project_id": project_id,
            "path": candidate.as_posix(),
            "reason": "path is not present in the active structural source index",
        }
    verified = _read_verified_indexed_source(repo_root, {
        "path": candidate.as_posix(),
        "file_content_hash": record.get("content_hash"),
    })
    if verified.get("status") != "ok":
        return {**verified, "status": "stale_source", "project_id": project_id}
    data = bytes(verified["data"])
    if str(record.get("language") or "").lower() == "python":
        try:
            text = _decode_python_source(data)
            source_encoding = "python-detected"
        except (LookupError, SyntaxError, UnicodeDecodeError):
            text = data.decode("utf-8", errors="replace")
            source_encoding = "utf-8-replace"
    else:
        text = data.decode("utf-8", errors="replace")
        source_encoding = "utf-8-replace"
    lines = text.splitlines()
    total_lines = len(lines)
    first = max(1, int(start_line or 1))
    requested_last = int(end_line or 0)
    if requested_last <= 0:
        requested_last = min(total_lines, first + 79)
    requested_last = max(first, min(requested_last, total_lines if total_lines else first))
    last = requested_last
    range_capped = False
    if last - first + 1 > 200:
        last = first + 199
        range_capped = True
    line_cap = max(128, min(int(max_line_chars or 4096), 16384))
    total_cap = max(500, min(int(max_chars or 20000), 48000))
    output: list[dict[str, Any]] = []
    used = 0
    total_truncated = False
    any_redacted = False
    for lineno in range(first, last + 1):
        raw = lines[lineno - 1] if 0 < lineno <= total_lines else ""
        safe_raw, line_redacted = safety.redact_source_text(raw)
        any_redacted = any_redacted or line_redacted
        preview = safe_raw[:line_cap]
        line_truncated = len(preview) < len(safe_raw)
        cost = len(preview.encode("utf-8", errors="replace")) + 32
        if output and used + cost > total_cap:
            total_truncated = True
            break
        if not output and cost > total_cap:
            preview = preview.encode("utf-8", errors="replace")[: max(1, total_cap - 64)].decode("utf-8", errors="ignore")
            line_truncated = True
            cost = len(preview.encode("utf-8")) + 32
        output.append({
            "line": lineno,
            "text": preview,
            "truncated": line_truncated,
            "source_chars": len(raw),
            "redacted": line_redacted,
        })
        used += cost
    returned_end = output[-1]["line"] if output else first - 1
    manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
    current_view = (
        provenance.light_view_state(
            repo_root,
            known_head=branch.commit_sha,
            exact_root_verified=branch.source in {"git_branch", "git_detached", "git_unknown"},
        )
        if branch.source_type == "git"
        else _source_evidence(repo_root, branch, deep=False)
    )
    current_view_fingerprint = str(current_view.get("view_fingerprint") or current_view.get("content_identity") or "")
    indexed_view_fingerprint = str((manifest or {}).get("repository_view_fingerprint") or "") if isinstance(manifest, dict) else ""
    indexed_assurance = ""
    if indexed_view_fingerprint and indexed_view_fingerprint == current_view_fingerprint and (
        branch.source_type != "git" or not branch.dirty
    ):
        indexed_assurance = str(((manifest or {}).get("repository_evidence") or {}).get("assurance") or "")
    indexed_repository_evidence = ((manifest or {}).get("repository_evidence") or {}) if isinstance(manifest, dict) else {}
    source_semantics_operations = _recommended_go_semantics_operations(
        "\n".join(lines[max(0, first - 1):max(0, returned_end)])
    )
    if branch.source_type == "git":
        evidence = provenance.build_source_evidence(
            repo_root=repo_root,
            project_id=project_id,
            repo_id=branch.repo_id,
            branch_key=branch.branch_key,
            commit_sha=branch.commit_sha,
            rel_path=candidate.as_posix(),
            source_sha256=str(verified.get("source_sha256") or ""),
            indexed_sha256=str(record.get("content_hash") or ""),
            start_line=output[0]["line"] if output else first,
            end_line=returned_end,
            assurance_hint=indexed_assurance,
            view_hint=current_view,
            snapshot_hint=indexed_repository_evidence if indexed_assurance else None,
        )
    else:
        evidence = provenance.build_corpus_source_evidence(
            project_id=project_id,
            source_id=branch.source_id,
            source_type=branch.source_type,
            revision_key=branch.revision_key,
            content_identity=branch.content_identity,
            rel_path=candidate.as_posix(),
            source_sha256=str(verified.get("source_sha256") or ""),
            start_line=output[0]["line"] if output else first,
            end_line=returned_end,
        )
    evidence_locator = EvidenceLocator(
        source_id=branch.source_id,
        revision_key=branch.revision_key,
        path=candidate.as_posix(),
        start_line=output[0]["line"] if output else first,
        end_line=returned_end,
    ).as_dict()
    line_truncated_lines = [int(row["line"]) for row in output if bool(row.get("truncated"))]
    range_incomplete = returned_end < requested_last
    truncation_reasons: list[str] = []
    if range_capped:
        truncation_reasons.append("max_lines")
    if total_truncated:
        truncation_reasons.append("max_chars")
    if line_truncated_lines:
        truncation_reasons.append("max_line_chars")
    truncated = bool(truncation_reasons or range_incomplete)
    if range_incomplete and not (range_capped or total_truncated):
        truncation_reasons.append("requested_range_incomplete")
    continue_from_line = returned_end + 1 if returned_end < requested_last else None
    suggested_action = ""
    if continue_from_line is not None:
        suggested_action = f"Continue with start_line={continue_from_line}, end_line={requested_last}."
    if line_truncated_lines:
        line_hint = f" Re-read clipped line(s) {line_truncated_lines[:8]} with a larger max_line_chars only if their omitted suffix is needed."
        suggested_action = (suggested_action + line_hint).strip()

    return {
        "status": "ok",
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "source_id": branch.source_id,
        "source_type": branch.source_type,
        "revision_key": branch.revision_key,
        "content_identity": branch.content_identity,
        "path": candidate.as_posix(),
        "source_role": indexing_policy.source_role(candidate.as_posix()),
        "branch_key": branch.branch_key,
        "branch_name": branch.branch_name,
        "commit_sha": branch.commit_sha,
        "dirty": branch.dirty,
        "source_sha256": verified.get("source_sha256"),
        "indexed_source_sha256": record.get("content_hash"),
        "evidence": evidence,
        "evidence_locator": evidence_locator,
        "deterministic_semantics": {
            "recommended": bool(source_semantics_operations),
            "tool": "code_semantics_check" if source_semantics_operations else "",
            "operations": source_semantics_operations,
            "reason": (
                "returned source contains a supported deterministic Go language/standard-library primitive; validate the concrete claim mechanically"
                if source_semantics_operations else ""
            ),
        },
        # ``requested`` remains the effective bounded range for compatibility;
        # the original requested end and explicit continuation metadata below
        # make truncation impossible to mistake for a complete source read.
        "requested": {"start_line": first, "end_line": last},
        "requested_original": {"start_line": first, "end_line": requested_last},
        "returned": {"start_line": output[0]["line"] if output else first, "end_line": returned_end},
        "total_lines": total_lines,
        "source_encoding": source_encoding,
        "lines": output,
        "redacted": any_redacted,
        "truncated": truncated,
        "truncation": {
            "truncated": truncated,
            "complete_requested_range": not range_incomplete and not line_truncated_lines,
            "reasons": truncation_reasons,
            "requested_end_line": requested_last,
            "effective_end_line": last,
            "returned_end_line": returned_end,
            "continue_from_line": continue_from_line,
            "line_truncated_lines": line_truncated_lines[:32],
            "suggested_action": suggested_action,
        },
        "limits": {"max_chars": total_cap, "max_line_chars": line_cap, "max_lines": 200},
        "rules": [
            "The full current file hash matched the active indexed source before this bounded window was returned.",
            "The evidence id binds the returned line range to the selected source revision and exact current source bytes; code_evidence_verify can detect later source or revision drift.",
            "The evidence id is a compact checksum-protected token for stale-evidence detection, not a cryptographic signature or authorship attestation.",
            "High-confidence credential literals are redacted from returned source text without excluding the surrounding source file from analysis.",
            "Long source lines are clipped explicitly rather than emitted unbounded.",
            "A bounded source window is authoritative for the returned bytes but does not itself prove runtime execution.",
        ],
    }


def verify_evidence(
    paths: Any, project_id: str, evidence_id: str, *, repo: str = "", source: str = ""
) -> dict[str, Any]:
    pp = project_workspace.paths_for(paths.root, project_id)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": project_id}
    try:
        payload = provenance.decode_evidence(evidence_id)
    except ValueError as exc:
        return {"status": "rejected", "project_id": project_id, "verdict": "INVALID_EVIDENCE", "reason": str(exc)}
    if str(payload.get("project_id") or "") != project_id:
        return {
            "status": "rejected", "project_id": project_id, "verdict": "PROJECT_MISMATCH",
            "reason": "evidence id belongs to a different Awoki project",
            "evidence_project_id": payload.get("project_id"),
        }

    # v5 evidence is bound to a generic content-manifest source rather than a
    # Git repository. Verification is deliberately independent of the code
    # index: current file bytes and the complete registered corpus manifest
    # must both still match the evidence token.
    if int(payload.get("v") or 0) == provenance.CORPUS_EVIDENCE_TOKEN_VERSION:
        evidence_source_id = str(payload.get("source_id") or "")
        if not evidence_source_id:
            return {"status": "rejected", "project_id": project_id, "verdict": "INVALID_EVIDENCE", "reason": "corpus evidence has no source identity"}
        if repo:
            return {"status": "rejected", "project_id": project_id, "verdict": "SOURCE_MISMATCH", "reason": "repo= cannot select non-Git source evidence"}
        if source and source != evidence_source_id:
            return {"status": "rejected", "project_id": project_id, "verdict": "SOURCE_MISMATCH", "reason": "explicit source does not match evidence source"}
        selected = project_workspace.resolve_project_source(paths.root, project_id, evidence_source_id)
        if selected.get("status") != "ok" or str(selected.get("source_type") or "git") == "git":
            return {
                "status": "rejected", "project_id": project_id, "verdict": "SOURCE_MISMATCH",
                "reason": "evidence source is not registered as the same non-Git source",
                "evidence_source_id": evidence_source_id,
            }
        source_root = Path(selected["root"])
        rel = str(payload.get("path") or "")
        candidate = Path(rel)
        if not rel or candidate.is_absolute() or ".." in candidate.parts:
            return {"status": "rejected", "project_id": project_id, "verdict": "INVALID_EVIDENCE", "reason": "evidence path is not safe"}
        allowed, policy_reason = indexing_policy.source_evidence_path_allowed(source_root / candidate, repo_root=source_root)
        if not allowed:
            return {
                "status": "rejected", "project_id": project_id, "verdict": "INVALID_EVIDENCE_POLICY",
                "reason": f"evidence path is not eligible for exact source evidence: {policy_reason}",
            }
        path = source_root / candidate
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("source is not a regular file")
            before = path.stat()
            if before.st_size > provenance.MAX_EVIDENCE_VERIFY_FILE_BYTES:
                return {
                    "status": "incomplete", "project_id": project_id, "verdict": "VERIFICATION_BUDGET_EXCEEDED",
                    "reason": f"source exceeds {provenance.MAX_EVIDENCE_VERIFY_FILE_BYTES} byte evidence verification limit",
                }
            data = path.read_bytes()
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise OSError("source changed while it was being read")
        except OSError as exc:
            return {"status": "stale", "project_id": project_id, "verdict": "STALE_SOURCE", "reason": str(exc), "path": rel}
        current_sha = _sha(data)
        current_identity = project_workspace.source_manifest_identity(source_root)
        current_content_identity = str(current_identity.get("content_identity") or "")
        expected_sha = str(payload.get("source_sha256") or "")
        expected_identity = str(payload.get("content_identity") or "")
        expected_revision = str(payload.get("revision_key") or "")
        source_current = current_sha == expected_sha
        revision_current = bool(
            expected_identity
            and current_content_identity == expected_identity
            and expected_revision == f"source:{evidence_source_id}|sha256:{current_content_identity}"
        )
        if not source_current:
            verdict = "STALE_SOURCE"
            reason = "current source bytes differ from the evidence id"
        elif not revision_current:
            verdict = "SOURCE_CURRENT_REVISION_CHANGED"
            reason = "source bytes still match, but another byte in the registered corpus changed"
        else:
            verdict = "CURRENT_SOURCE_CONTENT_MANIFEST_BOUND"
            reason = "source bytes and registered corpus content identity match the evidence id"
        return {
            "status": "ok" if source_current and revision_current else "stale",
            "project_id": project_id,
            "source_id": evidence_source_id,
            "source_type": selected.get("source_type"),
            "verdict": verdict,
            "reason": reason,
            "current": bool(source_current and revision_current),
            "source_current": source_current,
            "revision_current": revision_current,
            "path": rel,
            "evidence_assurance": "CONTENT_MANIFEST_BOUND",
            "evidence_authenticity": str(payload.get("authenticity") or "unknown"),
            "expected_source_sha256": expected_sha,
            "current_source_sha256": current_sha,
            "expected_content_identity": expected_identity,
            "current_content_identity": current_content_identity,
            "expected_revision_key": expected_revision,
            "current_revision_key": f"source:{evidence_source_id}|sha256:{current_content_identity}" if current_content_identity else "",
            "range": {"start_line": payload.get("start_line"), "end_line": payload.get("end_line")},
        }

    rows = project_workspace.project_repositories(paths.root, project_id)
    evidence_repo_id = str(payload.get("repo_id") or "")
    selected: dict[str, Any] | None = None
    if evidence_repo_id:
        for row in rows:
            branch = branch_identity(project_id, Path(row["root"]), repo_name=str(row["repo_id"]), legacy=bool(row.get("legacy")))
            if branch.repo_id == evidence_repo_id:
                selected = row
                break
        if selected is None:
            return {"status": "rejected", "project_id": project_id, "verdict": "REPOSITORY_MISMATCH", "reason": "evidence repository is not registered in this project", "evidence_repo_id": evidence_repo_id}
        if source and source != str(selected.get("repo_id")):
            return {"status": "rejected", "project_id": project_id, "verdict": "SOURCE_MISMATCH", "reason": "explicit source does not match evidence repository"}
        if repo and str(selected.get("repo_id")) != str(repo):
            return {"status": "rejected", "project_id": project_id, "verdict": "REPOSITORY_MISMATCH", "reason": "explicit repo does not match evidence repository"}
    else:
        # v3 did not encode repo identity. It remains safe for a legacy/single
        # repository project, but fails closed after migration to multi-repo.
        if len(rows) != 1:
            return {"status": "rejected", "project_id": project_id, "verdict": "AMBIGUOUS_LEGACY_EVIDENCE_REPOSITORY", "reason": "v3 evidence predates repository identity and cannot be safely verified in a multi-repo project"}
        selected = rows[0]
        if repo and str(selected.get("repo_id")) != str(repo):
            return {"status": "rejected", "project_id": project_id, "verdict": "REPOSITORY_MISMATCH", "reason": "explicit repo does not match legacy evidence repository"}
    assert selected is not None
    repo_root = Path(selected["root"])
    result = provenance.verify_source_evidence(repo_root, evidence_id)
    verdict = str(result.get("verdict") or "")
    if bool(result.get("current")):
        status = "ok"
    elif verdict.startswith("INVALID_EVIDENCE"):
        status = "rejected"
    elif verdict == "VERIFICATION_BUDGET_EXCEEDED":
        status = "incomplete"
    else:
        status = "stale"
    return {"status": status, "project_id": project_id, "repo_id": selected.get("repo_id"), "source_id": selected.get("repo_id"), "source_type": "git", **result}


def cross_project_search(
    paths: Any,
    query: str,
    *,
    projects: list[str],
    all_indexed: bool = False,
    mode: str = "auto",
    view: str = "context",
    limit: int = 20,
    refresh_stale: bool = False,
) -> dict[str, Any]:
    routing = route_query(query, mode)
    if all_indexed:
        selected = store.indexed_projects(paths.root)
    else:
        selected = [project_workspace.clean_project_id(value) for value in projects if str(value).strip()]
    selected = list(dict.fromkeys(selected))
    if not selected:
        return {
            "status": "rejected",
            "reason": "cross-project search requires an explicit project list or all_indexed=true",
        }
    per_project: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    each_limit = max(5, min(limit, 30))
    for project_id in selected:
        repositories = project_workspace.project_repositories(paths.root, project_id)
        project_hits: list[dict[str, Any]] = []
        repo_statuses: list[dict[str, Any]] = []
        for repository in repositories:
            rid = str(repository.get("repo_id") or "")
            result = search_project_code(
                paths,
                project_id,
                query,
                mode=mode,
                view=view,
                limit=each_limit,
                refresh_index=refresh_stale,
                include_qdrant=True,
                ensure_index=refresh_stale,
                repo=rid,
            )
            repo_hits = list(result.get("hits") or [])
            for hit in repo_hits:
                item = dict(hit)
                item.setdefault("repo_id", rid)
                project_hits.append(item)
            repo_statuses.append({
                "repo_id": rid,
                "status": result.get("status"),
                "scope": result.get("scope"),
                "freshness": result.get("freshness") or (result.get("index_status") or {}).get("freshness"),
                "hit_count": len(repo_hits),
                "reason": result.get("reason"),
            })
        if len(repo_statuses) == 1:
            project_status = str(repo_statuses[0].get("status") or "")
            project_reason = str(repo_statuses[0].get("reason") or "")
            project_scope = repo_statuses[0].get("scope")
            project_freshness = repo_statuses[0].get("freshness")
        else:
            project_status = "partial" if any(row.get("status") not in {"ok", "current", "indexed"} for row in repo_statuses) else "ok"
            project_reason = "project has no enabled repositories" if not repositories else ""
            project_scope = {"repositories": [row.get("repo_id") for row in repo_statuses], "multi_repo": True}
            project_freshness = None
        per_project.append({
            "project_id": project_id,
            "status": project_status,
            "scope": project_scope,
            "freshness": project_freshness,
            "hit_count": len(project_hits),
            "repositories": repo_statuses,
            "reason": project_reason,
        })
        # Scores from independent repository/project retrieval calls are not
        # directly comparable. Fuse by rank and retain source identity.
        project_hits.sort(key=lambda row: (-float(row.get("score") or 0.0), str(row.get("repo_id") or ""), str(row.get("path") or ""), int(row.get("start_line") or 0)))
        for project_rank, hit in enumerate(project_hits[:each_limit], start=1):
            item = dict(hit)
            item["project_rank"] = project_rank
            item["project_score"] = float(hit.get("score") or 0.0)
            item["score"] = 1.0 / (60.0 + project_rank)
            combined.append(item)
    combined.sort(key=lambda row: (
        int(row.get("project_rank") or 10**9),
        -float(row.get("project_score") or 0.0),
        str(row.get("project_id") or ""),
        str(row.get("path") or ""),
        int(row.get("start_line") or 0),
    ))
    return {
        "status": "ok",
        "query": query,
        "routing": routing,
        "scope": {
            "projects": selected,
            "all_indexed_explicit": bool(all_indexed),
        },
        "hits": combined[: max(1, min(limit, 100))],
        "projects": per_project,
        "rules": [
            "Cross-project scope was explicit.",
            "Every hit carries its project, repository, branch, path, and symbol identity.",
            "Project continuity and global memory are never searched by this operation.",
        ],
    }


def _passive_non_git_index_status(
    paths: Any, project_id: str, resolved: dict[str, Any]
) -> dict[str, Any]:
    """Report materialized non-Git state without walking or hashing the corpus.

    A content-manifest identity is only current after an explicit deep verify or
    an operation such as search/indexing that is allowed to inspect the source.
    Passive status therefore never upgrades a recorded corpus revision to a
    freshness claim merely because its registry entry still exists.
    """
    pp = project_workspace.paths_for(paths.root, project_id)
    source_root = Path(str(resolved.get("root") or ""))
    source_id = str(resolved.get("source_id") or "")
    source_type = str(resolved.get("source_type") or "corpus")
    db = store.db_path(pp.project_dir)
    if not source_root.exists():
        return {
            "status": "not_found",
            "project_id": project_id,
            "repo_id": "",
            "source_id": source_id,
            "source_type": source_type,
            "source_root": str(source_root),
            "repo_root": "",
            "reason": "source directory does not exist",
            "verification": {"mode": "passive", "repository_scan": False, "source_scan": False, "network": False},
        }

    state = store.state_for_source(db, project_id, source_id) if db.exists() else {}
    manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
    parser_profile = parser_runtime_profile()
    parser_hash = _json_hash(parser_profile)
    embedding_hash = vector_store.embedding_profile_hash()
    indexed_vector_collection = _published_vector_collection(manifest)
    current_vector_collection = vector_store.code_collection_name()
    indexed_repository_evidence = (manifest.get("repository_evidence") or {}) if isinstance(manifest, dict) else {}

    lexical_checks = {
        "source_probe": False,
        "repository_view": False,
        "parser_profile": bool(state) and state.get("parser_profile_hash") == parser_hash,
        "schema": bool(state) and int(state.get("schema_version") or 0) == store.SCHEMA_VERSION,
        "engine": bool(state) and state.get("engine_version") == ENGINE_VERSION,
    }
    vector_checks = {
        "lexical_current": False,
        "embedding_profile": bool(state) and state.get("embedding_profile_hash") == embedding_hash,
        "vector_collection": bool(indexed_vector_collection) and indexed_vector_collection == current_vector_collection,
        "vector_status": bool(state) and state.get("vector_status") == "indexed",
        "membership_recorded": bool(state) and bool(state.get("qdrant_membership_hash")),
    }
    stale_reasons = ["source_freshness_unverified"] if state else []
    stale_reasons.extend(name for name, current in lexical_checks.items() if name not in {"source_probe", "repository_view"} and not current)
    revision_key = str(state.get("revision_key") or state.get("branch_key") or "") if state else ""
    content_identity = str(state.get("content_identity") or "") if state else ""
    recorded_revision = SourceRevision(
        source_id=source_id,
        source_type=source_type,
        revision_key=revision_key,
        revision_label=str(state.get("revision_label") or "") if state else "",
        content_identity=content_identity,
        dirty=False,
        provenance={"identity_source": "recorded_materialized_state"},
        repo_id="",
        branch_key=revision_key,
        branch_name=str(state.get("revision_label") or "") if state else "",
        commit_sha="",
        source="passive_materialized_state",
    )
    recorded_excluded = (manifest.get("excluded") or []) if isinstance(manifest, dict) else []
    repository_evidence = {
        "status": "not_verified",
        "assurance": "CONTENT_MANIFEST_BOUND",
        "source_id": source_id,
        "source_type": source_type,
        "content_identity": "",
        "view_fingerprint": "",
        "limitations": [
            "Passive status does not walk or hash non-Git evidence sources.",
            "Run code_index_verify or a source-reading operation to establish current content-manifest identity.",
        ],
    }
    return {
        "status": "not_indexed" if not state else "unverified",
        "project_id": project_id,
        "repo_id": "",
        "source_id": source_id,
        "source_type": source_type,
        "source_root": str(source_root),
        "repo_root": "",
        "active_branch": recorded_revision.__dict__,
        "freshness": {
            "lexical_current": False,
            "vector_current": False,
            "checks": {**lexical_checks, "embedding_profile": vector_checks["embedding_profile"]},
            "lexical_checks": lexical_checks,
            "vector_checks": vector_checks,
            "indexed_vector_collection": indexed_vector_collection,
            "current_vector_collection": current_vector_collection,
            "stale_reasons": stale_reasons,
            "current_source_probe_hash": "",
            "indexed_source_probe_hash": str(state.get("source_probe_hash") or "") if state else "",
            "current_document_set_hash": "",
            "indexed_document_set_hash": str(state.get("document_set_hash") or "") if state else "",
            "current_membership_hash": "",
            "indexed_membership_hash": str(state.get("qdrant_membership_hash") or "") if state else "",
            "vector_status": state.get("vector_status") if state else None,
            "vector_reason": state.get("vector_reason") if state else None,
            "source_probe_verified_by": "not_deep_verified",
            "vector_live_verified": False,
        },
        "state": state,
        "repository_evidence": repository_evidence,
        "indexed_repository_evidence": indexed_repository_evidence,
        "repository_assurance": str(indexed_repository_evidence.get("assurance") or "CONTENT_MANIFEST_BOUND"),
        "counts": store.counts(db, revision_key) if state and revision_key else {},
        "known_branches": store.all_branches(db) if db.exists() else [],
        "manifest": manifest,
        "reference_integrity": (manifest.get("reference_integrity") or {}) if isinstance(manifest, dict) else {},
        "manifest_matches_active_branch": False,
        "parser_runtime": parser_profile,
        "qdrant_collection": vector_store.code_collection_name(),
        "current_excluded_count": len(recorded_excluded),
        "current_excluded_count_verified": False,
        "verification": {
            "mode": "passive",
            "repository_scan": False,
            "source_scan": False,
            "network": False,
            "source_freshness": "requires_explicit_verify",
            "qdrant_reachability": "not_probed",
        },
    }


def _passive_index_status(
    paths: Any, project_id: str, repo: str = "", source: str = ""
) -> dict[str, Any]:
    """Read materialized code-index state without source-wide scans or network I/O.

    Git content/index freshness is kept separate from mutable repository-view
    metadata. A clean unchanged content identity can remain current across an
    index/stat metadata drift, while bounded assurance probes still fail closed
    for status-suppressing flags or weakened Git stat trust.
    """
    resolved = _resolve_source_spec(paths, project_id, source=source, repo=repo, require_unique=True)
    if resolved.get("status") != "ok":
        return resolved
    if str(resolved.get("source_type") or "git") != "git":
        return _passive_non_git_index_status(paths, project_id, resolved)
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, db, resolved = context
    if not repo_root.exists():
        return {"status": "not_found", "project_id": project_id, "repo_id": resolved.get("repo_id"), "source_id": resolved.get("source_id"), "source_root": str(repo_root), "repo_root": str(repo_root) if branch.source_type == "git" else "", "reason": "source directory does not exist", "verification": {"mode": "passive", "repository_scan": False, "network": False}}
    if branch.source_type == "git" and branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_root": str(repo_root),
            "active_branch": branch.__dict__,
            "reason": "configured project repo/ is not the exact Git worktree root",
            "nested_git_roots": _nested_git_roots(repo_root),
            "verification": {"mode": "passive", "repository_scan": False, "network": False},
        }
    state = store.read_state(db, branch.branch_key) if db.exists() else {}
    manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
    parser_profile = parser_runtime_profile()
    parser_hash = _json_hash(parser_profile)
    embedding_hash = vector_store.embedding_profile_hash()
    indexed_vector_collection = _published_vector_collection(manifest)
    current_vector_collection = vector_store.code_collection_name()

    current_repository_evidence = _source_evidence(repo_root, branch, deep=False)
    indexed_repository_evidence = (manifest.get("repository_evidence") or {}) if isinstance(manifest, dict) else {}
    indexed_passive_reuse_safe = (
        provenance.passive_index_reuse_safe(indexed_repository_evidence)
        if branch.source_type == "git" else True
    )
    indexed_view_fingerprint = str((manifest or {}).get("repository_view_fingerprint") or "") if isinstance(manifest, dict) else ""
    current_view_fingerprint = str(current_repository_evidence.get("view_fingerprint") or "")
    indexed_content_view_fingerprint = str(
        (manifest or {}).get("repository_content_view_fingerprint")
        or provenance.content_view_fingerprint(indexed_repository_evidence)
    ) if isinstance(manifest, dict) else ""
    current_content_view_fingerprint = provenance.content_view_fingerprint(current_repository_evidence)
    repository_view_current = bool(indexed_view_fingerprint) and indexed_view_fingerprint == current_view_fingerprint
    content_view_current = bool(indexed_content_view_fingerprint) and indexed_content_view_fingerprint == current_content_view_fingerprint

    assurance_probe: dict[str, Any] = {
        "performed": False,
        "available": True,
        "reasons": [],
    }
    if branch.source_type == "git" and content_view_current and not repository_view_current:
        raw_flags = provenance.passive_index_flag_state(repo_root)
        assurance_reasons: list[str] = []
        if not raw_flags.get("available"):
            assurance_reasons.append("index_flags_unavailable")
        if int(raw_flags.get("assume_unchanged_count") or 0):
            assurance_reasons.append("assume_unchanged_index_entries")
        if int(raw_flags.get("skip_worktree_count") or 0) and not bool(current_repository_evidence.get("sparse_checkout")):
            assurance_reasons.append("manual_skip_worktree_index_entries")
        stat_trust = current_repository_evidence.get("git_stat_trust") or {}
        if bool(stat_trust.get("ignore_stat")):
            assurance_reasons.append("git_ignore_stat_active")
        if not bool(stat_trust.get("trust_ctime", True)):
            assurance_reasons.append("git_ctime_trust_disabled")
        if str(stat_trust.get("check_stat") or "default").lower() == "minimal":
            assurance_reasons.append("git_checkstat_minimal")
        assurance_probe = {
            "performed": True,
            "available": bool(raw_flags.get("available")),
            "assume_unchanged_count": int(raw_flags.get("assume_unchanged_count") or 0),
            "skip_worktree_count": int(raw_flags.get("skip_worktree_count") or 0),
            "reasons": sorted(set(assurance_reasons)),
        }
    assurance_reasons = list(assurance_probe.get("reasons") or [])
    current_assurance_safe = not assurance_reasons
    clean_git_snapshot = bool(
        branch.source_type == "git"
        and
        state
        and branch.source in {"git_branch", "git_detached"}
        and not branch.dirty
        and not bool(state.get("dirty"))
        and str(state.get("commit_sha") or "") == branch.commit_sha
        and indexed_passive_reuse_safe
        and content_view_current
        and current_assurance_safe
    )
    content_manifest_snapshot = bool(
        branch.source_type != "git"
        and state
        and str(state.get("source_id") or "") == branch.source_id
        and str(state.get("revision_key") or "") == branch.revision_key
        and str(state.get("content_identity") or "") == branch.content_identity
        and indexed_view_fingerprint == branch.content_identity
    )
    lexical_checks = {
        # A clean worktree at the same immutable commit and content-selection
        # identity proves source identity without reopening every eligible file.
        # Replacement refs/sparse-view changes invalidate content identity;
        # index/stat-only view drift remains diagnostic unless assurance falls.
        "source_probe": clean_git_snapshot or content_manifest_snapshot,
        "content_view": content_view_current if branch.source_type == "git" else repository_view_current,
        "parser_profile": bool(state) and state.get("parser_profile_hash") == parser_hash,
        "schema": bool(state) and int(state.get("schema_version") or 0) == store.SCHEMA_VERSION,
        "engine": bool(state) and state.get("engine_version") == ENGINE_VERSION,
    }
    lexical_current = bool(state) and all(lexical_checks.values())
    vector_checks = {
        "lexical_current": lexical_current,
        "embedding_profile": bool(state) and state.get("embedding_profile_hash") == embedding_hash,
        "vector_collection": bool(indexed_vector_collection) and indexed_vector_collection == current_vector_collection,
        "vector_status": bool(state) and state.get("vector_status") == "indexed",
        "membership_recorded": bool(state) and bool(state.get("qdrant_membership_hash")),
    }
    vector_current = bool(state) and all(vector_checks.values())
    stale_reasons = [name for name, current in lexical_checks.items() if not current]
    if lexical_current:
        stale_reasons.extend(name for name, current in vector_checks.items() if name != "lexical_current" and not current)
    status = (
        "not_indexed" if not state
        else "current" if lexical_current and vector_current
        else "degraded" if lexical_current
        else "stale"
    )
    recorded_excluded = (manifest.get("excluded") or []) if isinstance(manifest, dict) else []
    source_probe = str(state.get("source_probe_hash") or "") if state else ""
    return {
        "status": status,
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "source_id": resolved.get("source_id"),
        "source_type": branch.source_type,
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "active_branch": branch.__dict__,
        "freshness": {
            "lexical_current": lexical_current,
            "vector_current": vector_current,
            "checks": {
                **lexical_checks,
                "repository_view": repository_view_current,
                "embedding_profile": vector_checks["embedding_profile"],
            },
            "lexical_checks": lexical_checks,
            "vector_checks": vector_checks,
            "indexed_vector_collection": indexed_vector_collection,
            "current_vector_collection": current_vector_collection,
            "stale_reasons": stale_reasons,
            "view_drift_reasons": (
                [] if repository_view_current
                else ["repository_view_metadata"] if content_view_current
                else ["content_view"]
            ),
            "assurance_reasons": assurance_reasons,
            "repository_view_current": repository_view_current,
            "content_view_current": content_view_current,
            "indexed_repository_view_fingerprint": indexed_view_fingerprint,
            "current_repository_view_fingerprint": current_view_fingerprint,
            "indexed_content_view_fingerprint": indexed_content_view_fingerprint,
            "current_content_view_fingerprint": current_content_view_fingerprint,
            "assurance_probe": assurance_probe,
            "current_source_probe_hash": source_probe if (clean_git_snapshot or content_manifest_snapshot) else "",
            "indexed_source_probe_hash": source_probe,
            "current_document_set_hash": str(state.get("document_set_hash") or "") if state else "",
            "indexed_document_set_hash": str(state.get("document_set_hash") or "") if state else "",
            "current_membership_hash": str(state.get("qdrant_membership_hash") or "") if state else "",
            "indexed_membership_hash": str(state.get("qdrant_membership_hash") or "") if state else "",
            "vector_status": state.get("vector_status") if state else None,
            "vector_reason": state.get("vector_reason") if state else None,
            "source_probe_verified_by": (
                "clean_git_content_identity" if clean_git_snapshot
                else "content_manifest_identity" if content_manifest_snapshot
                else "not_deep_verified"
            ),
            "vector_live_verified": False,
        },
        "state": state,
        "repository_evidence": current_repository_evidence,
        "indexed_repository_evidence": indexed_repository_evidence,
        "repository_assurance": (
            str(indexed_repository_evidence.get("assurance") or ("WORKING_TREE_BOUND" if branch.source_type == "git" else "CONTENT_MANIFEST_BOUND"))
            if (clean_git_snapshot or content_manifest_snapshot)
            else str(current_repository_evidence.get("assurance") or ("WORKING_TREE_BOUND" if branch.source_type == "git" else "CONTENT_MANIFEST_BOUND"))
        ),
        "counts": store.counts(db, branch.branch_key) if state else {},
        "known_branches": store.all_branches(db) if db.exists() else [],
        "manifest": manifest,
        "reference_integrity": (manifest.get("reference_integrity") or {}) if isinstance(manifest, dict) else {},
        "manifest_matches_active_branch": bool(manifest) and manifest.get("branch_key") == branch.branch_key,
        "parser_runtime": parser_profile,
        "qdrant_collection": vector_store.code_collection_name(),
        # Compatibility field: this is the count recorded at indexing time. A
        # passive status call deliberately does not recompute the current set.
        "current_excluded_count": len(recorded_excluded),
        "current_excluded_count_verified": False,
        "verification": {
            "mode": "passive",
            "repository_scan": False,
            "network": False,
            "assurance_probe": bool(assurance_probe.get("performed")),
            "source_freshness": "proven" if (clean_git_snapshot or content_manifest_snapshot) else "requires_explicit_verify",
            "qdrant_reachability": "not_probed",
        },
    }


def _deep_index_status(
    paths: Any, project_id: str, *, verify_qdrant: bool = True, repo: str = "", source: str = ""
) -> dict[str, Any]:
    """Perform explicit source-wide and optional live-Qdrant verification."""
    context, error = _project_context_result(paths, project_id, repo=repo, source=source)
    if error:
        return error
    assert context is not None
    pp, repo_root, branch, db, resolved = context
    if branch.source_type == "git" and branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_root": str(repo_root),
            "active_branch": branch.__dict__,
            "reason": "configured project repo/ is not the exact Git worktree root",
            "nested_git_roots": _nested_git_roots(repo_root),
            "verification": {"mode": "deep", "repository_scan": False, "network": False},
        }
    state = store.read_state(db, branch.branch_key) if db.exists() else {}
    manifest = indexing_policy.read_index_manifest(_source_manifest_path(pp.project_dir, resolved))
    parser_profile = parser_runtime_profile()
    parser_hash = _json_hash(parser_profile)
    embedding_hash = vector_store.embedding_profile_hash()
    indexed_vector_collection = _published_vector_collection(manifest)
    current_vector_collection = vector_store.code_collection_name()
    current_repository_evidence = _source_evidence(repo_root, branch, deep=True)
    indexed_repository_evidence = (manifest.get("repository_evidence") or {}) if isinstance(manifest, dict) else {}
    indexed_view_fingerprint = str((manifest or {}).get("repository_view_fingerprint") or "") if isinstance(manifest, dict) else ""
    current_view_fingerprint = str(current_repository_evidence.get("view_fingerprint") or "")
    actual_git_snapshot = bool(
        branch.source_type == "git"
        and branch.source in {"git_branch", "git_detached", "git_unknown"}
    )
    if actual_git_snapshot:
        indexed_content_view_fingerprint = str(
            (manifest or {}).get("repository_content_view_fingerprint")
            or provenance.content_view_fingerprint(indexed_repository_evidence)
        ) if isinstance(manifest, dict) else ""
        current_content_view_fingerprint = provenance.content_view_fingerprint(current_repository_evidence)
    else:
        # Preserve the legacy filesystem-source contract. Some older fixtures
        # and registered sources are source_type=git even though the resolved
        # source revision is non_git; for them the repository/content identity
        # remains the lexical freshness key.
        indexed_content_view_fingerprint = str(
            indexed_view_fingerprint
            or ((manifest or {}).get("content_identity") if isinstance(manifest, dict) else "")
        )
        current_content_view_fingerprint = str(
            current_view_fingerprint
            or current_repository_evidence.get("content_identity")
            or branch.content_identity
        )
    repository_view_current = bool(indexed_view_fingerprint) and indexed_view_fingerprint == current_view_fingerprint
    content_view_current = bool(indexed_content_view_fingerprint) and indexed_content_view_fingerprint == current_content_view_fingerprint
    _, excluded, current_probe = _scan_repository(paths, project_id, repo_root) if repo_root.exists() else ([], [], "")
    rows = store.branch_embedding_memberships(db, project_id, branch.branch_key) if state else []
    current_document_hash = _document_set_hash(rows) if state else ""
    current_membership_hash = vector_store.membership_hash(rows, project_id, branch.branch_key) if state else ""
    lexical_checks = {
        "source_probe": bool(state) and state.get("source_probe_hash") == current_probe,
        "content_view": content_view_current,
        "parser_profile": bool(state) and state.get("parser_profile_hash") == parser_hash,
        "document_set": bool(state) and state.get("document_set_hash") == current_document_hash,
        "schema": bool(state) and int(state.get("schema_version") or 0) == store.SCHEMA_VERSION,
        "engine": bool(state) and state.get("engine_version") == ENGINE_VERSION,
    }
    lexical_current = bool(state) and all(lexical_checks.values())
    vector_checks = {
        "lexical_current": lexical_current,
        "embedding_profile": bool(state) and state.get("embedding_profile_hash") == embedding_hash,
        "vector_collection": bool(indexed_vector_collection) and indexed_vector_collection == current_vector_collection,
        "vector_status": bool(state) and state.get("vector_status") == "indexed",
        "membership": bool(state) and state.get("qdrant_membership_hash") == current_membership_hash,
    }
    vector_current = bool(state) and all(vector_checks.values())
    collection_reason = ""
    live_verified = False
    if vector_current and verify_qdrant:
        live_verified = True
        vector_current, collection_reason = vector_store.collection_available(timeout=2.0)
    stale_reasons = [name for name, current in lexical_checks.items() if not current]
    if lexical_current:
        stale_reasons.extend(name for name, current in vector_checks.items() if name != "lexical_current" and not current)
    if lexical_current and all(vector_checks.values()) and verify_qdrant and not vector_current:
        stale_reasons.append("vector_collection")
    status = "not_indexed" if not state else "current" if lexical_current and vector_current else "degraded" if lexical_current else "stale"
    return {
        "status": status,
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "source_id": resolved.get("source_id"),
        "source_type": branch.source_type,
        "source_root": str(repo_root),
        "repo_root": str(repo_root) if branch.source_type == "git" else "",
        "active_branch": branch.__dict__,
        "freshness": {
            "lexical_current": lexical_current,
            "vector_current": vector_current,
            "checks": {
                **lexical_checks,
                "repository_view": repository_view_current,
                "embedding_profile": vector_checks["embedding_profile"],
            },
            "lexical_checks": lexical_checks,
            "vector_checks": vector_checks,
            "indexed_vector_collection": indexed_vector_collection,
            "current_vector_collection": current_vector_collection,
            "stale_reasons": stale_reasons,
            "view_drift_reasons": (
                [] if repository_view_current
                else ["repository_view_metadata"] if content_view_current
                else ["content_view"]
            ),
            "repository_view_current": repository_view_current,
            "content_view_current": content_view_current,
            "indexed_repository_view_fingerprint": indexed_view_fingerprint,
            "current_repository_view_fingerprint": current_view_fingerprint,
            "indexed_content_view_fingerprint": indexed_content_view_fingerprint,
            "current_content_view_fingerprint": current_content_view_fingerprint,
            "current_source_probe_hash": current_probe,
            "indexed_source_probe_hash": state.get("source_probe_hash"),
            "current_document_set_hash": current_document_hash,
            "indexed_document_set_hash": state.get("document_set_hash"),
            "current_membership_hash": current_membership_hash,
            "indexed_membership_hash": state.get("qdrant_membership_hash"),
            "vector_status": state.get("vector_status"),
            "vector_reason": collection_reason or state.get("vector_reason"),
            "source_probe_verified_by": "repository_policy_hash_scan",
            "vector_live_verified": live_verified,
        },
        "state": state,
        "repository_evidence": current_repository_evidence,
        "indexed_repository_evidence": indexed_repository_evidence,
        "repository_assurance": str(current_repository_evidence.get("assurance") or ("WORKING_TREE_BOUND" if branch.source_type == "git" else "CONTENT_MANIFEST_BOUND")),
        "counts": store.counts(db, branch.branch_key) if state else {},
        "known_branches": store.all_branches(db) if db.exists() else [],
        "manifest": manifest,
        "reference_integrity": (manifest.get("reference_integrity") or {}) if isinstance(manifest, dict) else {},
        "manifest_matches_active_branch": bool(manifest) and manifest.get("branch_key") == branch.branch_key,
        "parser_runtime": parser_profile,
        "qdrant_collection": vector_store.code_collection_name(),
        "current_excluded_count": len(excluded),
        "current_excluded_count_verified": True,
        "verification": {
            "mode": "deep",
            "repository_scan": True,
            "network": bool(verify_qdrant and all(vector_checks.values())),
            "source_freshness": "verified",
            "qdrant_reachability": "verified" if live_verified else "not_requested_or_not_applicable",
        },
    }


def index_status(
    paths: Any,
    project_id: str,
    *,
    deep_verify: bool = False,
    verify_qdrant: bool = False,
    repo: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Return code-index status; passive by default, explicit deep verification opt-in."""
    if deep_verify:
        return _deep_index_status(
            paths, project_id, verify_qdrant=verify_qdrant, repo=repo, source=source
        )
    return _passive_index_status(paths, project_id, repo=repo, source=source)


def _claim_identifiers(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", text)
        if token.lower() not in {
            "the", "a", "an", "when", "where", "whose", "that", "then", "every",
            "raises", "raise", "returns", "return", "rejects", "reject", "throws", "throw",
            "calls", "call", "is", "by", "from", "to", "can", "reach", "defined", "in",
            "if", "and", "or", "not", "true", "false", "none",
        }
    }


def _read_verified_indexed_source(
    repo_root: Path,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Read exactly the indexed file or return a conservative stale result."""
    rel_path = str(definition.get("path") or "")
    if not rel_path:
        return {"status": "stale", "verdict": "STALE_SOURCE", "reason": "indexed definition has no source path"}
    path = repo_root / rel_path
    try:
        resolved_repo = repo_root.resolve(strict=True)
        if path.is_symlink():
            return {
                "status": "stale",
                "verdict": "STALE_SOURCE",
                "reason": "indexed source is now a symlink",
                "path": rel_path,
            }
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_repo)
        before = path.stat()
        if not path.is_file():
            return {
                "status": "stale",
                "verdict": "STALE_SOURCE",
                "reason": "indexed source is no longer a regular file",
                "path": rel_path,
            }
        data = path.read_bytes()
        after = path.stat()
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {
            "status": "stale",
            "verdict": "STALE_SOURCE",
            "reason": f"indexed source could not be read safely: {exc}",
            "path": rel_path,
        }
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(data) != after.st_size:
        return {
            "status": "stale",
            "verdict": "STALE_SOURCE",
            "reason": "indexed source changed while it was being read",
            "path": rel_path,
        }
    source_hash = _sha(data)
    indexed_hash = str(definition.get("file_content_hash") or "")
    if indexed_hash and indexed_hash != source_hash:
        return {
            "status": "stale",
            "verdict": "STALE_SOURCE",
            "reason": "current source bytes differ from the indexed and policy-validated file",
            "path": rel_path,
            "source_sha256": source_hash,
            "indexed_sha256": indexed_hash,
        }
    return {
        "status": "ok",
        "path": path,
        "relative_path": rel_path,
        "data": data,
        "source_sha256": source_hash,
        "indexed_sha256": indexed_hash,
    }


def _decode_python_source(data: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def _find_python_function(tree: ast.AST, symbol: str, start_line: int) -> ast.AST | None:
    simple = symbol.rsplit(".", 1)[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == simple:
            if int(getattr(node, "lineno", 0)) == start_line:
                return node
    return None


def _find_python_symbol_table(table: symtable.SymbolTable, symbol: str, start_line: int) -> symtable.SymbolTable | None:
    simple = symbol.rsplit(".", 1)[-1]
    for child in table.get_children():
        if child.get_name() == simple and int(child.get_lineno() or 0) == int(start_line):
            return child
        nested = _find_python_symbol_table(child, simple, start_line)
        if nested is not None:
            return nested
    return None


def _assignment_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            names.update(_assignment_names(item))
    elif isinstance(node, ast.Starred):
        names.update(_assignment_names(node.value))
    return names


def _module_name_is_rebound(tree: ast.Module, name: str, definition_line: int) -> bool:
    """Conservatively detect explicit ways the module binding can change.

    Module-level control-flow bodies still bind in the module namespace. Local
    function/class bodies do not, but an explicit ``global name`` anywhere in
    them means runtime code can replace the binding, so strict validation must
    stop rather than assume that mutation never occurs.
    """

    class BindingVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.module_scope = True
            self.rebound = False

        def _visit_definition_expressions(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in [*node.args.defaults, *[value for value in node.args.kw_defaults if value is not None]]:
                self.visit(default)
            for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            if node.args.vararg and node.args.vararg.annotation is not None:
                self.visit(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                self.visit(node.args.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if self.module_scope and node.name == name and int(getattr(node, "lineno", 0)) != definition_line:
                self.rebound = True
            self._visit_definition_expressions(node)
            if any(isinstance(child, ast.Global) and name in child.names for child in ast.walk(node)):
                self.rebound = True
            # Function body has its own local scope.

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if self.module_scope and node.name == name and int(getattr(node, "lineno", 0)) != definition_line:
                self.rebound = True
            for expression in [*node.decorator_list, *node.bases, *[item.value for item in node.keywords]]:
                self.visit(expression)
            if any(isinstance(child, ast.Global) and name in child.names for child in ast.walk(node)):
                self.rebound = True
            # Ordinary class-body assignments bind the class namespace, not the
            # module. Explicit global declarations above are treated as unsafe.

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for default in [*node.args.defaults, *[value for value in node.args.kw_defaults if value is not None]]:
                self.visit(default)
            # Lambda body has its own local scope.

        def visit_Name(self, node: ast.Name) -> None:
            if self.module_scope and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
                self.rebound = True

        def visit_Import(self, node: ast.Import) -> None:
            if self.module_scope:
                for alias in node.names:
                    if (alias.asname or alias.name.split(".", 1)[0]) == name:
                        self.rebound = True

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.module_scope:
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        self.rebound = True

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if self.module_scope and node.name == name:
                self.rebound = True
            self.generic_visit(node)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if self.module_scope and node.name == name:
                self.rebound = True
            self.generic_visit(node)

        def visit_Global(self, node: ast.Global) -> None:
            if name in node.names:
                self.rebound = True

    visitor = BindingVisitor()
    visitor.visit(tree)
    return visitor.rebound


def _direct_calls_in_function(function: ast.AST, target: str) -> list[ast.Call]:
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.matches: list[ast.Call] = []

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == target:
                self.matches.append(node)
            self.generic_visit(node)

        # Nested definitions are separate lexical/execution units and must not
        # be attributed to their containing function.
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    visitor = Visitor()
    for statement in list(getattr(function, "body", [])):
        visitor.visit(statement)
    return visitor.matches


def _validate_python_direct_call(
    repo_root: Path,
    caller: dict[str, Any],
    callee: dict[str, Any],
) -> dict[str, Any]:
    """Prove a narrow same-file Python source-level direct call obligation.

    This deliberately refuses imports, receiver dispatch, parameter/local
    shadowing, decorators, and module rebinding. Those are possible graph edges,
    but not strong enough for `/code-validate-claim` to report VERIFIED.
    """
    if caller.get("path") != callee.get("path"):
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "cross-file Python calls require import/binding resolution beyond the strict source-level proof profile",
        }
    current = _read_verified_indexed_source(repo_root, caller)
    if current.get("status") != "ok":
        return {key: value for key, value in current.items() if key not in {"status", "data", "path"}}
    callee_hash = str(callee.get("file_content_hash") or "")
    source_hash = str(current["source_sha256"])
    if callee_hash and callee_hash != source_hash:
        return {
            "verdict": "STALE_SOURCE",
            "reason": "caller and callee index records do not refer to the same current file bytes",
            "source_sha256": source_hash,
            "indexed_sha256": callee_hash,
        }
    path = Path(current["path"])
    source_bytes = bytes(current["data"])
    try:
        source = _decode_python_source(source_bytes)
        tree = ast.parse(source, filename=str(path), type_comments=True)
        symbols = symtable.symtable(source, str(path), "exec")
    except (SyntaxError, UnicodeError, ValueError) as exc:
        return {"verdict": "INCONCLUSIVE", "reason": f"Python source analysis failed: {exc}", "source_sha256": source_hash}
    caller_node = _find_python_function(tree, str(caller.get("symbol_name") or ""), int(caller.get("start_line") or 0))
    callee_node = _find_python_function(tree, str(callee.get("symbol_name") or ""), int(callee.get("start_line") or 0))
    if caller_node is None or callee_node is None:
        return {"verdict": "INCONCLUSIVE", "reason": "indexed caller or callee could not be re-resolved in current source", "source_sha256": source_hash}
    if list(getattr(caller_node, "decorator_list", []) or []) or list(getattr(callee_node, "decorator_list", []) or []):
        return {"verdict": "INCONCLUSIVE", "reason": "decorators may replace or wrap the caller/callee binding", "source_sha256": source_hash}

    callee_name = str(callee.get("symbol_name") or "")
    calls = _direct_calls_in_function(caller_node, callee_name)
    if not calls:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "the fresh caller AST contains no direct bare-name call to the requested callee",
            "source_sha256": source_hash,
        }
    caller_table = _find_python_symbol_table(symbols, str(caller.get("symbol_name") or ""), int(caller.get("start_line") or 0))
    if caller_table is None:
        return {"verdict": "INCONCLUSIVE", "reason": "Python lexical scope table could not be resolved", "source_sha256": source_hash}
    try:
        binding = caller_table.lookup(callee_name)
    except KeyError:
        binding = None

    # A direct nested definition is a deterministic local binding. Any other
    # local/parameter/import/nonlocal binding could point to arbitrary runtime
    # data and therefore cannot prove the indexed callee is selected.
    nested_definition = any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == callee_name
        and int(getattr(statement, "lineno", 0)) == int(callee.get("start_line") or 0)
        for statement in list(getattr(caller_node, "body", []))
    )
    if binding is not None:
        if binding.is_parameter() or binding.is_imported() or binding.is_nonlocal() or (binding.is_local() and not nested_definition):
            return {
                "verdict": "INCONCLUSIVE",
                "reason": "the call name is a parameter, import, nonlocal, or other local binding and cannot be tied to the indexed callee without assumption",
                "binding": {
                    "parameter": binding.is_parameter(),
                    "imported": binding.is_imported(),
                    "nonlocal": binding.is_nonlocal(),
                    "local": binding.is_local(),
                    "global": binding.is_global(),
                },
                "source_sha256": source_hash,
            }

    if not nested_definition:
        # A bare name in Python function scope resolves through lexical/global
        # bindings, never through the containing class namespace. Therefore the
        # only non-local definition accepted by this strict profile is a direct
        # module-level function in the same file.
        module_level_callee = any(statement is callee_node for statement in tree.body)
        if not module_level_callee:
            return {
                "verdict": "INCONCLUSIVE",
                "reason": "the requested bare-name callee is not a direct module-level definition under the strict Python proof profile",
                "source_sha256": source_hash,
            }
        if _module_name_is_rebound(tree, callee_name, int(callee.get("start_line") or 0)):
            return {
                "verdict": "INCONCLUSIVE",
                "reason": "the module explicitly rebinds the callee name",
                "source_sha256": source_hash,
            }

    evidence = [
        {
            "path": str(caller.get("path") or ""),
            "caller": str(caller.get("qualified_name") or caller.get("symbol_name") or ""),
            "callee": str(callee.get("qualified_name") or callee_name),
            "call": safety.redact_source_text(ast.get_source_segment(source, call) or ast.unparse(call))[0],
            "line": int(getattr(call, "lineno", 0)),
            "column": int(getattr(call, "col_offset", 0)),
            "source_sha256": source_hash,
        }
        for call in calls
    ]
    return {
        "verdict": "VERIFIED",
        "reason": "fresh Python AST and lexical-scope analysis prove a direct same-scope source-level call without local shadowing, imports, decorators, or explicit module rebinding",
        "evidence": evidence,
        "source_sha256": source_hash,
    }


def _parse_condition_obligation(text: str) -> tuple[ast.AST | None, str]:
    value = text.strip().rstrip(".")
    translations = (
        (r"^(.+?)\s+differs\s+from\s+(.+)$", r"\1 != \2"),
        (r"^(.+?)\s+is\s+not\s+equal\s+to\s+(.+)$", r"\1 != \2"),
        (r"^(.+?)\s+does\s+not\s+equal\s+(.+)$", r"\1 != \2"),
        (r"^(.+?)\s+equals\s+(.+)$", r"\1 == \2"),
        (r"^(.+?)\s+is\s+equal\s+to\s+(.+)$", r"\1 == \2"),
    )
    for pattern, replacement in translations:
        if re.match(pattern, value, flags=re.IGNORECASE):
            value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
            break
    try:
        return ast.parse(value, mode="eval").body, value
    except SyntaxError:
        return None, value


def _parse_outcome_obligation(verb: str, text: str) -> tuple[ast.AST | None, str]:
    value = text.strip().rstrip(".")
    # Natural-language articles do not carry program semantics.
    value = re.sub(r"^(?:an?|the)\s+", "", value, flags=re.IGNORECASE)
    try:
        expression = ast.parse(value, mode="eval").body
    except SyntaxError:
        return None, value
    return expression, value


def _is_docstring_statement(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _validate_python_condition_claim(repo_root: Path, definition: dict[str, Any], claim: str) -> dict[str, Any]:
    """Verify a deliberately narrow source-level condition/outcome obligation.

    VERIFIED is returned only when the exact condition is a top-level first
    executable statement in the unique function and its first branch statement
    is the exact requested return/raise. Any extra control dependency or earlier
    executable statement makes the result INCONCLUSIVE rather than guessed.
    """
    current = _read_verified_indexed_source(repo_root, definition)
    if current.get("status") != "ok":
        return {key: value for key, value in current.items() if key not in {"status", "data", "path"}}
    path = Path(current["path"])
    source_bytes = bytes(current["data"])
    source_hash = str(current["source_sha256"])
    try:
        source = _decode_python_source(source_bytes)
        tree = ast.parse(source, filename=str(path), type_comments=True)
    except (SyntaxError, UnicodeError) as exc:
        message = exc.msg if isinstance(exc, SyntaxError) else str(exc)
        return {"verdict": "INCONCLUSIVE", "reason": f"python parser failed: {message}", "source_sha256": source_hash}
    function = _find_python_function(tree, str(definition["symbol_name"]), int(definition["start_line"]))
    if function is None:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "indexed symbol could not be re-resolved in the current source",
            "source_sha256": source_hash,
        }
    match = re.search(
        r"^(?P<symbol>[A-Za-z_$][A-Za-z0-9_$.]*)\s+"
        r"(?P<verb>rejects|raises|returns|throws)\s+"
        r"(?P<outcome>.+?)\s+when\s+(?P<condition>.+)$",
        claim.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "claim is not in a supported deterministic condition/outcome form",
            "supported_form": "<symbol> raises|returns|rejects <exact outcome> when <exact condition>",
            "source_sha256": source_hash,
        }
    verb = match.group("verb").lower()
    condition_node, normalized_condition = _parse_condition_obligation(match.group("condition"))
    outcome_node, normalized_outcome = _parse_outcome_obligation(verb, match.group("outcome"))
    if condition_node is None or outcome_node is None:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "condition or outcome could not be translated into an exact Python AST obligation",
            "normalized_condition": normalized_condition,
            "normalized_outcome": normalized_outcome,
            "source_sha256": source_hash,
        }
    required_condition = ast.dump(condition_node, annotate_fields=True, include_attributes=False)
    required_outcome = ast.dump(outcome_node, annotate_fields=True, include_attributes=False)
    parent_of: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            parent_of[child] = parent

    body = list(getattr(function, "body", []))
    executable = [statement for statement in body if not _is_docstring_statement(statement)]
    first_executable = executable[0] if executable else None
    candidates: list[dict[str, Any]] = []
    expected_statement = ast.Raise if verb in {"raises", "throws", "rejects"} else ast.Return

    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        observed_condition = ast.dump(node.test, annotate_fields=True, include_attributes=False)
        if observed_condition != required_condition:
            continue
        blockers: list[str] = []
        decorators = list(getattr(function, "decorator_list", []) or [])
        if decorators:
            blockers.append("decorators may replace or wrap the inspected function at runtime")
        parent = parent_of.get(node)
        while parent is not None and parent is not function:
            if isinstance(parent, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Match, ast.Try, ast.With, ast.AsyncWith, ast.ExceptHandler)):
                blockers.append(f"additional enclosing control dependency: {type(parent).__name__}")
            parent = parent_of.get(parent)
        if first_executable is not node:
            blockers.append("the matching condition is not the function's first executable statement")
        branch_statements = [statement for statement in node.body if not _is_docstring_statement(statement)]
        if not branch_statements:
            blockers.append("the matching branch has no executable outcome")
            observed_outcome_node: ast.AST | None = None
            observed_statement: ast.stmt | None = None
        else:
            observed_statement = branch_statements[0]
            if not isinstance(observed_statement, expected_statement):
                blockers.append("the requested outcome is not the branch's first executable statement")
                observed_outcome_node = None
            elif isinstance(observed_statement, ast.Raise):
                observed_outcome_node = observed_statement.exc
            else:
                observed_outcome_node = observed_statement.value
        observed_outcome = (
            ast.dump(observed_outcome_node, annotate_fields=True, include_attributes=False)
            if observed_outcome_node is not None else ""
        )
        if observed_outcome != required_outcome:
            blockers.append("the first branch outcome does not match the exact requested AST")
        evidence = {
            "path": str(definition["path"]),
            "source_sha256": source_hash,
            "condition": safety.redact_source_text(ast.get_source_segment(source, node.test) or ast.unparse(node.test))[0],
            "condition_line": int(getattr(node, "lineno", 0)),
            "condition_ast": observed_condition,
            "required_condition_ast": required_condition,
            "outcome": (
                safety.redact_source_text(ast.get_source_segment(source, observed_statement) or ast.unparse(observed_statement))[0]
                if observed_statement is not None else ""
            ),
            "outcome_line": int(getattr(observed_statement, "lineno", 0)) if observed_statement is not None else 0,
            "outcome_ast": observed_outcome,
            "required_outcome_ast": required_outcome,
            "blockers": blockers,
        }
        candidates.append(evidence)
        if not blockers:
            return {
                "verdict": "VERIFIED",
                "reason": "the unique function definition satisfies the exact condition/outcome proof obligations without an additional static control dependency",
                "evidence": evidence,
                "source_sha256": source_hash,
            }
    return {
        "verdict": "INCONCLUSIVE",
        "reason": "no source branch satisfied every exact condition/outcome proof obligation",
        "candidates": candidates[:20],
        "source_sha256": source_hash,
    }


def validate_claim(
    paths: Any,
    project_id: str,
    claim: str,
    *,
    refresh_index: bool = False,
    repo: str = "",
) -> dict[str, Any]:
    """Validate a constrained code claim without embeddings or reranking."""
    if not claim.strip():
        return {"status": "rejected", "reason": "claim cannot be empty"}
    index = ensure_current(paths, project_id, include_qdrant=False, force=refresh_index, repo=repo)
    if index.get("status") not in {"indexed", "current"}:
        return index
    pp, repo_root, branch, db, resolved = _project_context(paths, project_id, repo)
    source_fingerprint = {
        "project_id": project_id,
        "repo_id": resolved.get("repo_id"),
        "branch_key": branch.branch_key,
        "commit_sha": branch.commit_sha,
        "dirty": branch.dirty,
        "document_set_hash": store.read_state(db, branch.branch_key).get("document_set_hash"),
    }
    normalized = " ".join(claim.strip().split())

    call_match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$.]*)\s+calls\s+([A-Za-z_$][A-Za-z0-9_$.]*)$", normalized, re.IGNORECASE)
    called_by_match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$.]*)\s+is\s+called\s+by\s+([A-Za-z_$][A-Za-z0-9_$.]*)$", normalized, re.IGNORECASE)
    defined_match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$.]*)\s+is\s+defined\s+in\s+(.+)$", normalized, re.IGNORECASE)
    reach_match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$.]*)\s+(?:can\s+)?reach(?:es)?\s+([A-Za-z_$][A-Za-z0-9_$.]*)$", normalized, re.IGNORECASE)

    if call_match or called_by_match:
        caller_name, callee_name = (call_match.group(1), call_match.group(2)) if call_match else (called_by_match.group(2), called_by_match.group(1))
        caller_defs = store.definitions(db, project_id, branch.branch_key, caller_name, 20)
        callee_defs = store.definitions(db, project_id, branch.branch_key, callee_name, 20)
        if len(caller_defs) != 1 or len(callee_defs) != 1:
            return {
                "status": "validated",
                "verdict": "AMBIGUOUS_SYMBOL",
                "claim": claim,
                "caller_candidates": len(caller_defs),
                "callee_candidates": len(callee_defs),
                "source": source_fingerprint,
            }
        caller = caller_defs[0]
        callee = callee_defs[0]
        edges = store.callees(db, branch.branch_key, str(caller["symbol_id"]))
        exact = [
            row for row in edges
            if row.get("target_symbol_id") == callee["symbol_id"]
            and row.get("resolution_status") == "resolved"
        ]
        if caller.get("language") == "python" and callee.get("language") == "python":
            proof = _validate_python_direct_call(repo_root, caller, callee)
        else:
            proof = {
                "verdict": "INCONCLUSIVE",
                "reason": (
                    "strict direct-call proof currently requires fresh Python AST and lexical-scope analysis; "
                    "the generic structural graph is navigation evidence only"
                ),
            }
        return {
            "status": "validated",
            "claim": claim,
            **proof,
            "graph_evidence": exact,
            "source": source_fingerprint,
            "method": [
                "exact definition lookup",
                "fresh source hash verification",
                "fresh Python AST reparse",
                "lexical-scope and shadowing analysis",
                "structural graph used only as supporting evidence",
            ],
            "certainty_boundary": (
                "VERIFIED proves a direct source-level call in the inspected, hashed Python source under the "
                "strict binding profile. It does not prove that a production runtime reaches the caller."
            ),
        }

    if reach_match:
        source_name, target_name = reach_match.group(1), reach_match.group(2)
        source_defs = store.definitions(db, project_id, branch.branch_key, source_name, 20)
        target_defs = store.definitions(db, project_id, branch.branch_key, target_name, 20)
        if len(source_defs) != 1 or len(target_defs) != 1:
            return {
                "status": "validated",
                "verdict": "AMBIGUOUS_SYMBOL" if source_defs or target_defs else "INCONCLUSIVE",
                "claim": claim,
                "reason": "deterministic path validation requires one exact source definition and one exact target definition",
                "source_candidates": len(source_defs),
                "target_candidates": len(target_defs),
                "source": source_fingerprint,
            }
        graph = store.graph_path(
            db, branch.branch_key,
            [str(source_defs[0]["symbol_id"])],
            {str(target_defs[0]["symbol_id"])},
        )
        edge_proofs: list[dict[str, Any]] = []
        if graph.get("status") == "found":
            for edge in graph.get("path", []):
                caller = store.symbol_by_id(db, str(edge.get("source_symbol_id") or ""))
                callee = store.symbol_by_id(db, str(edge.get("target_symbol_id") or ""))
                if not caller or not callee:
                    edge_proofs.append({
                        "verdict": "INCONCLUSIVE",
                        "reason": "a graph endpoint could not be re-resolved in the current structural index",
                        "edge": edge,
                    })
                    continue
                if caller.get("language") == "python" and callee.get("language") == "python":
                    proof = _validate_python_direct_call(repo_root, caller, callee)
                else:
                    proof = {
                        "verdict": "INCONCLUSIVE",
                        "reason": "strict path-edge proof currently requires Python AST and lexical-scope analysis",
                    }
                edge_proofs.append({
                    "caller": caller.get("qualified_name") or caller.get("symbol_name"),
                    "callee": callee.get("qualified_name") or callee.get("symbol_name"),
                    **proof,
                })
        all_verified = bool(edge_proofs) and all(item.get("verdict") == "VERIFIED" for item in edge_proofs)
        return {
            "status": "validated",
            "verdict": "VERIFIED" if all_verified else "INCONCLUSIVE",
            "claim": claim,
            "reason": (
                "every edge in the bounded path was re-proved from fresh Python source without ambiguous binding"
                if all_verified
                else "a possible structural path exists only when graph edges are present, but one or more edges could not be proved without assumption"
            ),
            "graph_evidence": graph,
            "edge_proofs": edge_proofs,
            "source": source_fingerprint,
            "method": [
                "exact definition lookup",
                "bounded structural path discovery",
                "fresh source hash verification for every edge",
                "fresh Python AST and lexical-scope proof for every edge",
            ],
            "certainty_boundary": (
                "VERIFIED proves a source-level chain of direct calls in the inspected, hashed Python source. "
                "It does not prove that a production entry point invokes the first function or that external/runtime effects occur."
            ),
        }

    if defined_match:
        symbol, expected_path = defined_match.group(1), defined_match.group(2).strip().strip("`\"")
        definitions = store.definitions(db, project_id, branch.branch_key, symbol, 20)
        if len(definitions) != 1:
            return {
                "status": "validated",
                "verdict": "AMBIGUOUS_SYMBOL" if definitions else "INCONCLUSIVE",
                "claim": claim,
                "reason": "deterministic location validation requires one exact symbol definition",
                "candidates": len(definitions),
                "evidence": definitions,
                "source": source_fingerprint,
                "method": ["exact symbol index", "active-branch path comparison"],
            }
        definition = definitions[0]
        current = _read_verified_indexed_source(repo_root, definition)
        if current.get("status") != "ok":
            return {
                "status": "validated",
                "claim": claim,
                **{key: value for key, value in current.items() if key not in {"status", "data", "path"}},
                "source": source_fingerprint,
                "method": ["exact symbol index", "fresh source hash verification"],
            }
        actual_path = str(definition.get("path") or "")
        matched = actual_path == expected_path or actual_path.endswith(expected_path)
        return {
            "status": "validated",
            "verdict": "VERIFIED" if matched else "REFUTED",
            "claim": claim,
            "evidence": [{**definition, "source_sha256": current.get("source_sha256")}],
            "source": source_fingerprint,
            "method": ["exact symbol index", "fresh source hash verification", "active-branch path comparison"],
            "certainty_boundary": "The verdict is limited to the unique definition in the inspected active-branch source snapshot.",
        }

    behavior_match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$.]*)\s+(rejects|raises|returns|throws)\s+", normalized, re.IGNORECASE)
    if behavior_match:
        symbol = behavior_match.group(1)
        definitions = store.definitions(db, project_id, branch.branch_key, symbol, 20)
        if len(definitions) != 1:
            return {
                "status": "validated",
                "verdict": "AMBIGUOUS_SYMBOL" if definitions else "INCONCLUSIVE",
                "claim": claim,
                "candidates": len(definitions),
                "source": source_fingerprint,
            }
        definition = definitions[0]
        if definition.get("language") != "python":
            return {
                "status": "validated",
                "verdict": "INCONCLUSIVE",
                "claim": claim,
                "reason": "deterministic branch/outcome proof is currently implemented for Python ASTs; other languages retain graph evidence only",
                "definition": _bounded_rows(db, definitions, "context", 1),
                "source": source_fingerprint,
            }
        result = _validate_python_condition_claim(repo_root, definition, normalized)
        return {
            "status": "validated",
            "claim": claim,
            **result,
            "definition": _bounded_rows(db, definitions, "context", 1),
            "source": source_fingerprint,
            "method": ["exact symbol lookup", "fresh source reparse", "Python AST control-flow inspection", "exact identifier obligations"],
            "certainty_boundary": "No embeddings, reranking, or semantic synonym guesses were used.",
        }

    return {
        "status": "validated",
        "verdict": "INCONCLUSIVE",
        "claim": claim,
        "reason": "claim could not be translated into a supported deterministic proof obligation",
        "supported_claims": [
            "X calls Y",
            "X is called by Y",
            "X can reach Y",
            "X is defined in path/to/file",
            "X raises|returns|rejects OUTCOME when EXACT_CONDITION",
        ],
        "source": source_fingerprint,
    }
