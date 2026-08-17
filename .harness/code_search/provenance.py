from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import zlib
from pathlib import Path
from typing import Any
import runtime_safety

import indexing_policy

EVIDENCE_TOKEN_VERSION = 4
CORPUS_EVIDENCE_TOKEN_VERSION = 5
MAX_GIT_OUTPUT = 256_000
MAX_EVIDENCE_TOKEN_CHARS = 8192
MAX_EVIDENCE_PAYLOAD_BYTES = 16384
MAX_EVIDENCE_VERIFY_FILE_BYTES = 64 * 1024 * 1024
MAX_PROVENANCE_AUX_FILE_BYTES = 4 * 1024 * 1024
GIT_REPOSITORY_ENV_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)
GIT_TRANSIENT_CONFIG_ENV = (
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_EXTERNAL_DIFF",
    "GIT_SHALLOW_FILE",
    "GIT_QUARANTINE_PATH",
    "GIT_GRAFT_FILE",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_ATTR_NOSYSTEM",
)


PASSIVE_REUSE_BLOCKERS = {
    "assume_unchanged_index_entries",
    "manual_skip_worktree_index_entries",
    "git_ignore_stat_active",
    "git_ctime_trust_disabled",
    "git_checkstat_minimal",
    "git_index_changed_during_verification",
}


def passive_index_reuse_safe(evidence: dict[str, Any] | None) -> bool:
    """Whether a clean same-view Git index may be reused without a full scan.

    Reduced provenance assurance is not automatically a source-freshness
    failure. Sparse checkout, replacement refs, or explicit submodule scope can
    lower provenance while the indexed visible source remains reusable when the
    exact mutable view is unchanged. Status-suppressing index flags and weakened
    Git stat trust are different: they can hide worktree byte changes, so those
    states require explicit/local revalidation rather than passive reuse.
    """
    evidence = dict(evidence or {})
    if evidence.get("status") != "git" or not evidence.get("view_fingerprint"):
        return False
    anomalies = {str(item) for item in evidence.get("anomalies") or []}
    return not bool(anomalies & PASSIVE_REUSE_BLOCKERS)


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="replace")
    return hashlib.sha256(value).hexdigest()


def _bounded_regular_bytes(path: Path, *, limit: int = MAX_PROVENANCE_AUX_FILE_BYTES) -> tuple[bytes, str]:
    """Read a bounded regular file without following symlinks/special files."""
    try:
        if path.is_symlink() or not path.is_file():
            return b"", "not_regular_file"
        before = path.stat()
        if before.st_size > limit:
            return b"", "too_large"
        data = path.read_bytes()
        after = path.stat()
        stable = (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns
            and int(getattr(before, "st_ino", 0)) == int(getattr(after, "st_ino", 0))
        )
        if not stable:
            return b"", "changed_during_read"
        return data, "ok"
    except OSError:
        return b"", "unreadable"


def _bounded_regular_text(path: Path, *, limit: int = MAX_PROVENANCE_AUX_FILE_BYTES) -> tuple[str, str]:
    data, state = _bounded_regular_bytes(path, limit=limit)
    if state != "ok":
        return "", state
    return data.decode("utf-8", errors="replace"), "ok"


def _git_env(*, no_replace: bool = False) -> tuple[dict[str, str], list[str]]:
    env = runtime_safety.credential_free_environment()
    ignored: list[str] = []
    for key in GIT_REPOSITORY_ENV_OVERRIDES:
        if key in env:
            ignored.append(key)
            env.pop(key, None)
    for key in GIT_TRANSIENT_CONFIG_ENV:
        env.pop(key, None)
    for key in list(env):
        if key.startswith("GIT_TRACE"):
            env.pop(key, None)
    for key in ("GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(key, None)
    # GIT_CONFIG_COUNT/GIT_CONFIG_KEY_N/GIT_CONFIG_VALUE_N can inject arbitrary
    # per-process configuration. Repository/global config remains visible, but
    # transient caller overrides cannot silently rebind the view Awoki audits.
    for key in list(env):
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = ""
    if no_replace:
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env, ignored


def sanitized_git_environment(*, no_replace: bool = False) -> dict[str, str]:
    """Environment for deterministic repository-local Git reads."""
    env, _ = _git_env(no_replace=no_replace)
    return env


def _run_git(
    repo_root: Path,
    *args: str,
    timeout: float = 8.0,
    no_replace: bool = False,
) -> tuple[int, str]:
    env, _ = _git_env(no_replace=no_replace)
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout[:MAX_GIT_OUTPUT].rstrip("\r\n")


def _run_git_bytes(
    repo_root: Path,
    *args: str,
    timeout: float = 8.0,
    no_replace: bool = False,
) -> tuple[int, bytes]:
    env, _ = _git_env(no_replace=no_replace)
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    return completed.returncode, completed.stdout[:MAX_GIT_OUTPUT]


def _filter_neutralizing_config(repo_root: Path) -> list[str]:
    """Return ``git -c`` args that prevent configured content filters running.

    A clean/smudge/process filter is arbitrary local executable code. Passive
    repository inspection must not execute it. Empty driver overrides make Git
    compare literal worktree/index representations instead. That comparison can
    conservatively report a filtered checkout as dirty, but it cannot hide
    source or claim more assurance than we actually established.
    """
    args: list[str] = []
    for name in _configured_filters(repo_root):
        args.extend([
            "-c", f"filter.{name}.process=",
            "-c", f"filter.{name}.clean=",
            "-c", f"filter.{name}.smudge=",
            "-c", f"filter.{name}.required=false",
        ])
    return args


def _run_git_status_bytes(repo_root: Path, *args: str, timeout: float = 12.0) -> tuple[int, bytes, list[str]]:
    env, _ = _git_env()
    filters = _configured_filters(repo_root)
    config_args: list[str] = []
    for name in filters:
        config_args.extend([
            "-c", f"filter.{name}.process=",
            "-c", f"filter.{name}.clean=",
            "-c", f"filter.{name}.smudge=",
            "-c", f"filter.{name}.required=false",
        ])
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *config_args, "-C", str(repo_root), "status", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, b"", filters
    return completed.returncode, completed.stdout[:MAX_GIT_OUTPUT], filters


def _safe_resolve(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _parse_identity_line(value: str) -> dict[str, Any]:
    # Git commit author/committer fields are claims embedded in the commit
    # object. They are not identity verification unless a separately verified
    # signature establishes stronger provenance.
    match = re.match(r"^(?P<name>.*) <(?P<email>[^>]*)> (?P<timestamp>-?\d+) (?P<tz>[+-]\d{4})$", value)
    if not match:
        return {"raw": value[:1000], "verified": False, "source": "git_commit_metadata"}
    return {
        "name": match.group("name")[:500],
        "email": match.group("email")[:500],
        "timestamp": match.group("timestamp"),
        "timezone": match.group("tz"),
        "verified": False,
        "source": "git_commit_metadata",
    }


def _raw_commit(repo_root: Path, head: str) -> dict[str, Any]:
    if not head:
        return {}
    rc, text = _run_git(repo_root, "cat-file", "-p", head, no_replace=True)
    if rc != 0:
        return {}
    tree = ""
    parents: list[str] = []
    author: dict[str, Any] = {}
    committer: dict[str, Any] = {}
    signature_present = False
    for line in text.splitlines():
        if not line:
            break
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = _parse_identity_line(line[7:])
        elif line.startswith("committer "):
            committer = _parse_identity_line(line[10:])
        elif line.startswith("gpgsig ") or line.startswith("gpgsig-sha256 "):
            signature_present = True
    return {
        "tree_sha": tree,
        "parent_shas": parents,
        "author_claim": author,
        "committer_claim": committer,
        "signature_present": signature_present,
    }


def _replace_refs(repo_root: Path) -> list[dict[str, str]]:
    base = str(os.environ.get("GIT_REPLACE_REF_BASE") or "refs/replace").strip().rstrip("/") or "refs/replace"
    rc, output = _run_git(
        repo_root,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        base,
    )
    if rc != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        ref, _, oid = line.partition("\t")
        rows.append({"ref": ref[:1000], "replacement_object": oid[:256]})
    return rows[:64]


def _status_summary(repo_root: Path) -> dict[str, Any]:
    rc, raw, configured_filters = _run_git_status_bytes(repo_root, "--porcelain=v1", "-z", "--untracked-files=normal", timeout=12.0)
    if rc != 0:
        return {"available": False, "dirty": True}
    entries = [item for item in raw.split(b"\0") if item]
    staged = 0
    worktree = 0
    untracked = 0
    conflict = 0
    for item in entries:
        if len(item) < 3:
            continue
        x = chr(item[0])
        y = chr(item[1])
        if x == "?" and y == "?":
            untracked += 1
            continue
        if x not in {" ", "?"}:
            staged += 1
        if y not in {" ", "?"}:
            worktree += 1
        if x == "U" or y == "U" or (x, y) in {("A", "A"), ("D", "D")}:
            conflict += 1
    return {
        "available": True,
        "dirty": bool(entries),
        "entry_count": len(entries),
        "staged_count": staged,
        "worktree_count": worktree,
        "untracked_count": untracked,
        "conflict_count": conflict,
        "content_filters_neutralized": bool(configured_filters),
        "configured_filter_drivers": configured_filters,
        "cleanliness_proof": "literal_worktree_index_no_filters" if configured_filters else "git_status",
    }


def _bool_config(repo_root: Path, key: str) -> bool:
    rc, value = _run_git(repo_root, "config", "--bool", "--get", key)
    return rc == 0 and value.strip().lower() == "true"


def _configured_filters(repo_root: Path) -> list[str]:
    rc, output = _run_git(repo_root, "config", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$")
    if rc != 0:
        return []
    names: set[str] = set()
    for line in output.splitlines():
        key = line.split(None, 1)[0] if line.strip() else ""
        match = re.match(r"^filter\.(.+)\.(?:clean|smudge|process|required)$", key)
        if match and match.group(1):
            names.add(match.group(1)[:256])
    return sorted(names)[:64]


def _configured_filter_keys(repo_root: Path) -> list[str]:
    """Return configured filter keys, never command values.

    Git filter command values are intentionally not surfaced because they may
    contain local paths, shell fragments, or secrets.  The keys are enough to
    explain why a passive cleanliness proof was withheld.
    """
    rc, output = _run_git(repo_root, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$")
    if rc != 0:
        return []
    return sorted({line.strip()[:500] for line in output.splitlines() if line.strip()})[:128]


def configured_filter_names(repo_root: Path) -> list[str]:
    """Return configured Git content-filter driver names without executing them."""
    return _configured_filters(repo_root)


def configured_filter_keys(repo_root: Path) -> list[str]:
    """Return configured Git content-filter key names without command values."""
    return _configured_filter_keys(repo_root)


def active_configured_filter_names(repo_root: Path) -> list[str]:
    """Return configured filter drivers referenced by active attribute files."""
    return list(_active_configured_filters(repo_root).get("active") or [])


def passive_status_porcelain(repo_root: Path, *, untracked: str = "normal") -> dict[str, Any]:
    """Read porcelain status without executing configured filters/fsmonitor."""
    mode = untracked if untracked in {"no", "normal", "all"} else "normal"
    rc, raw, filters = _run_git_status_bytes(repo_root, "--porcelain=v1", "-z", f"--untracked-files={mode}")
    if rc != 0:
        return {
            "available": False,
            "dirty": True,
            "entries": [],
            "configured_filter_drivers": filters,
        }
    entries = [item for item in raw.split(b"\0") if item]
    return {
        "available": True,
        "dirty": bool(entries),
        "entries": [os.fsdecode(item) for item in entries],
        "configured_filter_drivers": filters,
        "content_filters_neutralized": bool(filters),
        "cleanliness_proof": "literal_worktree_index_no_filters" if filters else "git_status",
    }


def passive_git_identity(repo_root: Path, *, untracked: str = "normal") -> dict[str, Any]:
    """Read HEAD/branch plus dirty entries in one filter-neutralized status.

    Porcelain v2 branch headers let the hot code-search path avoid separate
    ``rev-parse HEAD`` and ``branch --show-current`` subprocesses.
    """
    mode = untracked if untracked in {"no", "normal", "all"} else "normal"
    rc, raw, filters = _run_git_status_bytes(
        repo_root,
        "--porcelain=v2",
        "--branch",
        "-z",
        f"--untracked-files={mode}",
    )
    if rc != 0:
        return {
            "available": False,
            "dirty": True,
            "commit_sha": "",
            "branch_name": "",
            "entries": [],
            "configured_filter_drivers": filters,
        }
    commit = ""
    branch = ""
    entries: list[str] = []
    for item in (part for part in raw.split(b"\0") if part):
        text = os.fsdecode(item)
        if text.startswith("# branch.oid "):
            oid = text[len("# branch.oid "):].strip()
            commit = "" if oid == "(initial)" else oid
        elif text.startswith("# branch.head "):
            head = text[len("# branch.head "):].strip()
            branch = "" if head in {"(detached)", "(unknown)"} else head
        elif not text.startswith("# "):
            entries.append(text)
    return {
        "available": True,
        "dirty": bool(entries),
        "commit_sha": commit,
        "branch_name": branch,
        "entries": entries,
        "configured_filter_drivers": filters,
        "content_filters_neutralized": bool(filters),
        "cleanliness_proof": "literal_worktree_index_no_filters" if filters else "git_status",
    }


def _attribute_filters(repo_root: Path) -> dict[str, Any]:
    rc, raw = _run_git_bytes(repo_root, "ls-files", "-z", "--", ".gitattributes", ":(glob)**/.gitattributes")
    if rc != 0:
        return {"paths": [], "filter_names": []}
    paths = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    filters: set[str] = set()
    used_paths: list[str] = []
    for rel in paths[:128]:
        candidate = repo_root / rel
        text, state = _bounded_regular_text(candidate)
        if state != "ok":
            continue
        used_paths.append(Path(rel).as_posix())
        for match in re.finditer(r"(?:^|\s)filter=([^\s#]+)", text):
            filters.add(match.group(1)[:128])
    # Repository-local and global attribute files can also activate filters.
    # Read their text directly instead of asking Git to convert any source
    # file through the configured filter machinery.
    extra_paths: list[Path] = []
    git_dir_rc, git_dir = _run_git(repo_root, "rev-parse", "--git-dir")
    if git_dir_rc == 0 and git_dir:
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = repo_root / git_dir_path
        extra_paths.append(git_dir_path / "info" / "attributes")
    attrs_rc, attrs_file = _run_git(repo_root, "config", "--path", "--get", "core.attributesFile")
    if attrs_rc == 0 and attrs_file:
        extra_paths.append(Path(os.path.expanduser(attrs_file)))
    for candidate in extra_paths:
        text, state = _bounded_regular_text(candidate)
        if state != "ok":
            continue
        used_paths.append(_safe_resolve(candidate))
        for match in re.finditer(r"(?:^|\s)filter=([^\s#]+)", text):
            filters.add(match.group(1)[:128])
    return {"paths": used_paths[:256], "filter_names": sorted(filters)}


def _active_configured_filters(repo_root: Path) -> dict[str, Any]:
    """Conservatively identify configured filters referenced by attributes.

    Merely having an unrelated filter configured globally should not make every
    repository look dirty.  Conversely, once repository/local/global
    attributes reference a configured driver, passive ``git status`` may run
    that driver's clean/process command.  We therefore withhold the cheap
    cleanliness proof rather than execute it behind the user's back.
    """
    configured = set(_configured_filters(repo_root))
    attributes = _attribute_filters(repo_root)
    referenced = set(attributes.get("filter_names") or [])
    active = sorted(configured & referenced)
    return {
        "configured": sorted(configured),
        "referenced": sorted(referenced),
        "active": active,
        "attribute_paths": list(attributes.get("paths") or []),
    }


def _submodule_evidence(repo_root: Path, *, deep: bool) -> dict[str, Any]:
    rc, raw = _run_git_bytes(repo_root, "ls-files", "--stage", "-z", timeout=12.0)
    if rc != 0:
        return {"gitlink_count": 0, "gitlinks": [], "status_verified": False}
    gitlinks: list[dict[str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        left, sep, path_raw = item.partition(b"\t")
        if not sep:
            continue
        parts = left.decode("ascii", errors="ignore").split()
        if len(parts) >= 3 and parts[0] == "160000":
            gitlinks.append({"path": os.fsdecode(path_raw), "object": parts[1]})
    result: dict[str, Any] = {
        "gitlink_count": len(gitlinks),
        "gitlinks": gitlinks[:128],
        "status_verified": False,
    }
    if deep and gitlinks:
        states: list[dict[str, str]] = []
        for row in gitlinks[:128]:
            rel = str(row.get("path") or "")
            expected = str(row.get("object") or "")
            sub_root = repo_root / rel
            top_rc, top = _run_git(sub_root, "rev-parse", "--show-toplevel") if sub_root.exists() else (1, "")
            try:
                exact = top_rc == 0 and bool(top) and _safe_resolve(Path(top)) == _safe_resolve(sub_root)
            except OSError:
                exact = False
            head_rc, head = _run_git(sub_root, "rev-parse", "HEAD") if exact else (1, "")
            state = "missing"
            if exact and head_rc == 0:
                state = "at_gitlink" if head == expected else "different_head"
            states.append({"state": state, "object": head[:256], "expected_object": expected[:256], "path": rel[:2000]})
        result["status_verified"] = True
        result["worktree_status"] = states
        result["all_at_gitlink"] = bool(states) and all(row.get("state") == "at_gitlink" for row in states)
        result["recursive_dirty_state_verified"] = False
    elif not gitlinks:
        result["all_at_gitlink"] = True
    return result


def _unmaterialized_tracked(repo_root: Path) -> dict[str, Any]:
    rc, raw = _run_git_bytes(repo_root, "ls-files", "-z", timeout=12.0)
    if rc != 0:
        return {"count": 0, "sample": [], "verified": False}
    missing: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = os.fsdecode(item)
        path = repo_root / rel
        if not path.exists() and not path.is_symlink():
            missing.append(Path(rel).as_posix())
    return {"count": len(missing), "sample": missing[:32], "verified": True}


def _alternates(repo_root: Path, git_common_dir: str) -> dict[str, Any]:
    values: list[str] = []
    ignored_environment: list[str] = []
    for key in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        if os.environ.get(key):
            ignored_environment.append(key)
    common = Path(git_common_dir)
    if not common.is_absolute():
        common = repo_root / common
    alternates = common / "objects" / "info" / "alternates"
    text, state = _bounded_regular_text(alternates)
    if state == "ok" and any(line.strip() for line in text.splitlines()):
        values.append("objects/info/alternates")
    elif state not in {"ok", "not_regular_file"}:
        values.append(f"objects/info/alternates:{state}")
    return {
        "present": bool(values),
        "sources": sorted(set(values)),
        "ignored_environment_overrides": sorted(ignored_environment),
    }


def _history_view(repo_root: Path, git_common_dir: str, *, replace_refs_present: bool) -> dict[str, Any]:
    """Describe local history-view limitations without contacting a remote.

    This deliberately does not run fsck or fetch.  It can establish that the
    currently reachable graph is shallow/grafted/replaced, but it cannot prove
    that rewritten or unreachable commits never existed elsewhere.
    """
    shallow_rc, shallow_raw = _run_git(repo_root, "rev-parse", "--is-shallow-repository")
    shallow = shallow_rc == 0 and shallow_raw.strip().lower() == "true"
    common = Path(git_common_dir)
    if not common.is_absolute():
        common = repo_root / common
    grafts = common / "info" / "grafts"
    grafts_present = False
    grafts_sha256 = ""
    graft_bytes, graft_state = _bounded_regular_bytes(grafts)
    if graft_state == "ok" and graft_bytes:
        grafts_present = True
        grafts_sha256 = _sha(graft_bytes)
    elif graft_state not in {"ok", "not_regular_file"}:
        grafts_present = True
        grafts_sha256 = graft_state

    promisor_rc, promisor_keys = _run_git(
        repo_root, "config", "--name-only", "--get-regexp", r"^remote\..*\.promisor$"
    )
    promisor_configured = promisor_rc == 0 and bool(promisor_keys.strip())
    partial_rc, partial_remote = _run_git(repo_root, "config", "--get", "extensions.partialClone")
    partial_clone = promisor_configured or (partial_rc == 0 and bool(partial_remote.strip()))

    altered = bool(shallow or grafts_present or replace_refs_present or partial_clone)
    return {
        "history_assurance": "LIMITED_OR_REWRITTEN_LOCAL_VIEW" if altered else "CURRENT_REACHABLE_LOCAL_VIEW",
        "shallow_repository": shallow,
        "grafts_present": grafts_present,
        "grafts_sha256": grafts_sha256,
        "replace_refs_present": bool(replace_refs_present),
        "partial_clone_configured": bool(partial_clone),
        "promisor_remote_configured": bool(promisor_configured),
        "lazy_fetch_disabled": True,
        "remote_contacted": False,
        "limitations": [
            "Local history inspection cannot prove that rewritten, unreachable, garbage-collected, or remote-only commits never existed.",
            "Awoki disables lazy promisor-object fetching during passive/deep evidence inspection; missing objects reduce what can be established instead of causing network access.",
        ],
    }


def _content_view_fingerprint(payload: dict[str, Any]) -> str:
    """Hash only Git state that can select different content at the same HEAD.

    Mutable index stat identity and stat-trust configuration are assurance/view
    metadata, not corpus identity. Replacement refs and sparse-view selection stay
    in this hash because they can change the effective source bytes at a fixed HEAD.
    """
    stable = {
        "head_sha": payload.get("head_sha", ""),
        "replace_refs": payload.get("replace_refs", []),
        "replace_ref_base": payload.get("replace_ref_base", "refs/replace"),
        "sparse_checkout": payload.get("sparse_checkout", False),
        "sparse_cone": payload.get("sparse_cone", False),
        "sparse_patterns_sha256": payload.get("sparse_patterns_sha256", ""),
        "git_no_replace_objects": payload.get("git_no_replace_objects", False),
    }
    return _sha(json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def content_view_fingerprint(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict) or str(payload.get("status") or "") != "git":
        return ""
    explicit = str(payload.get("content_view_fingerprint") or "")
    return explicit or _content_view_fingerprint(payload)


def _view_fingerprint(payload: dict[str, Any]) -> str:
    # The hot-path view fingerprint contains only state that can mutate the
    # meaning/materialization of the same HEAD without changing HEAD itself.
    # Raw/effective tree IDs and object format are still recorded by deep
    # evidence, but recomputing them on every code question would add needless
    # Git subprocesses while replacement refs already identify tree rewrites.
    stable = {
        "head_sha": payload.get("head_sha", ""),
        "replace_refs": payload.get("replace_refs", []),
        "replace_ref_base": payload.get("replace_ref_base", "refs/replace"),
        "sparse_checkout": payload.get("sparse_checkout", False),
        "sparse_cone": payload.get("sparse_cone", False),
        "sparse_patterns_sha256": payload.get("sparse_patterns_sha256", ""),
        "git_no_replace_objects": payload.get("git_no_replace_objects", False),
        "git_index_identity": payload.get("git_index_identity", ""),
        "ignore_stat": payload.get("ignore_stat", False),
        "trust_ctime": payload.get("trust_ctime", True),
        "check_stat": payload.get("check_stat", "default"),
    }
    return _sha(json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _git_dir_from_marker(repo_root: Path) -> Path | None:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    try:
        if marker.is_file():
            text, state = _bounded_regular_text(marker, limit=64 * 1024)
            if state != "ok":
                return None
            lines = text.splitlines()
            if not lines:
                return None
            first = lines[0].strip()
            if first.lower().startswith("gitdir:"):
                value = first.split(":", 1)[1].strip()
                path = Path(value)
                if not path.is_absolute():
                    path = repo_root / path
                return path
    except (OSError, IndexError):
        return None
    return None


def _git_view_config(repo_root: Path) -> dict[str, Any]:
    """Read Git configuration that can weaken clean-worktree/view assumptions."""
    rc, output = _run_git(
        repo_root,
        "config",
        "--get-regexp",
        r"^core\.(sparseCheckout|sparseCheckoutCone|ignoreStat|trustctime|checkStat)$",
    )
    values: dict[str, str] = {}
    if rc == 0:
        for line in output.splitlines():
            key, _, value = line.partition(" ")
            values[key.strip().lower()] = value.strip().lower()
    sparse = values.get("core.sparsecheckout") == "true"
    return {
        "sparse_checkout": sparse,
        "sparse_cone": sparse and values.get("core.sparsecheckoutcone") == "true",
        "ignore_stat": values.get("core.ignorestat") == "true",
        "trust_ctime": values.get("core.trustctime", "true") != "false",
        "check_stat": values.get("core.checkstat", "default") or "default",
    }


def _sparse_patterns_hash(repo_root: Path, *, sparse: bool) -> str:
    if not sparse:
        return ""
    git_dir_path = _git_dir_from_marker(repo_root)
    if git_dir_path is None:
        return "unknown"
    candidate = git_dir_path / "info" / "sparse-checkout"
    data, state = _bounded_regular_bytes(candidate)
    if state == "ok":
        return _sha(data)
    if state == "not_regular_file":
        return "missing"
    return state


def _git_index_state(repo_root: Path, *, content_hash: bool = False) -> dict[str, Any]:
    """Describe the local index without executing Git helpers.

    Hot paths bind a cheap stat identity rather than re-hashing a potentially
    large monorepo index on every question. Explicit deep verification streams
    the bytes and re-stats each file afterwards; a concurrent mutation is
    reported instead of producing a falsely stable byte anchor.
    """
    git_dir = _git_dir_from_marker(repo_root)
    if git_dir is None:
        return {"available": False, "identity": "", "sha256": "", "split_index_files": 0, "stable": False}
    candidates: list[Path] = [git_dir / "index"]
    try:
        candidates.extend(sorted(git_dir.glob("sharedindex.*"), key=lambda item: item.name)[:64])
    except OSError:
        pass
    stat_rows: list[dict[str, Any]] = []
    digest = hashlib.sha256() if content_hash else None
    split = 0
    stable = True
    try:
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            before = candidate.stat()
            row = {
                "name": candidate.name,
                "size": int(before.st_size),
                "mtime_ns": int(before.st_mtime_ns),
                "ctime_ns": int(before.st_ctime_ns),
                "inode": int(getattr(before, "st_ino", 0)),
            }
            stat_rows.append(row)
            if candidate.name.startswith("sharedindex."):
                split += 1
            if digest is not None:
                digest.update(candidate.name.encode("utf-8", errors="replace"))
                digest.update(b"\0")
                with candidate.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                digest.update(b"\0")
                after = candidate.stat()
                if not (
                    before.st_size == after.st_size
                    and before.st_mtime_ns == after.st_mtime_ns
                    and before.st_ctime_ns == after.st_ctime_ns
                    and int(getattr(before, "st_ino", 0)) == int(getattr(after, "st_ino", 0))
                ):
                    stable = False
    except OSError:
        return {
            "available": False,
            "identity": "unreadable",
            "sha256": "unreadable" if content_hash else "",
            "split_index_files": split,
            "stable": False,
        }
    identity = _sha(json.dumps(stat_rows, sort_keys=True, separators=(",", ":"))) if stat_rows else ""
    return {
        "available": bool(stat_rows),
        "identity": identity,
        "sha256": digest.hexdigest() if digest is not None and stat_rows and stable else ("unstable" if digest is not None and stat_rows else ""),
        "split_index_files": split,
        "file_count": len(stat_rows),
        "stable": stable if content_hash else True,
    }


def _index_flag_evidence(repo_root: Path) -> dict[str, Any]:
    """Describe status-suppressing tracked-path index flags without hiding files."""
    rc, raw = _run_git_bytes(repo_root, "ls-files", "-v", "-z", timeout=12.0)
    if rc != 0:
        return {"available": False, "assume_unchanged_count": 0, "skip_worktree_count": 0}
    assume: list[str] = []
    skip: list[str] = []
    for item in raw.split(b"\0"):
        if len(item) < 3 or item[1:2] != b" ":
            continue
        tag = chr(item[0])
        rel = os.fsdecode(item[2:])
        # `git ls-files -v` lowercases the normal tracked tag for
        # assume-unchanged entries. Skip-worktree is tagged S (or s when also
        # assume-unchanged).
        if tag == "S" or tag == "s":
            skip.append(rel)
        if tag.islower():
            assume.append(rel)
    return {
        "available": True,
        "assume_unchanged_count": len(assume),
        "assume_unchanged_sample": assume[:32],
        "skip_worktree_count": len(skip),
        "skip_worktree_sample": skip[:32],
    }


def passive_index_flag_state(repo_root: Path) -> dict[str, Any]:
    """Bounded passive probe for status-suppressing index flags."""
    return _index_flag_evidence(Path(repo_root))


def light_view_state(
    repo_root: Path,
    *,
    known_head: str = "",
    exact_root_verified: bool = False,
) -> dict[str, Any]:
    """Return the cheap Git *content view* identity used on hot search paths.

    This deliberately does not run ``git status`` or inspect authorship,
    submodules, filters, signatures, or the full tracked-file inventory.  Those
    checks belong to explicit index/verify work.  The light identity is only
    used to notice mutations that can change what the same HEAD resolves to:
    replacement refs and sparse-view state in particular.
    """
    repo_root = Path(repo_root)
    if not repo_root.exists():
        return {
            "status": "missing",
            "view_fingerprint": _sha(f"missing\0{_safe_resolve(repo_root)}"),
        }
    if not exact_root_verified:
        top_rc, top = _run_git(repo_root, "rev-parse", "--show-toplevel")
        if top_rc != 0 or not top:
            return {
                "status": "non_git",
                "view_fingerprint": _sha(f"filesystem\0{_safe_resolve(repo_root)}"),
            }
        if _safe_resolve(Path(top)) != _safe_resolve(repo_root):
            payload = {
                "status": "invalid_repo_root",
                "git_top_level": _safe_resolve(Path(top)),
                "repo_root": _safe_resolve(repo_root),
            }
            payload["view_fingerprint"] = _sha(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            return payload

    head = known_head
    if not head:
        head_rc, resolved_head = _run_git(repo_root, "rev-parse", "HEAD")
        head = resolved_head if head_rc == 0 else ""
    replacements = _replace_refs(repo_root)
    view_config = _git_view_config(repo_root)
    sparse = bool(view_config.get("sparse_checkout"))
    sparse_cone = bool(view_config.get("sparse_cone"))
    index_state = _git_index_state(repo_root)
    payload = {
        "status": "git",
        "head_sha": head,
        "replace_refs": replacements,
        "replace_refs_present": bool(replacements),
        "replace_ref_base": str(os.environ.get("GIT_REPLACE_REF_BASE") or "refs/replace"),
        "git_no_replace_objects": str(os.environ.get("GIT_NO_REPLACE_OBJECTS") or "").strip().lower() not in {"", "0", "false", "no"},
        "sparse_checkout": sparse,
        "sparse_cone": sparse_cone,
        "sparse_patterns_sha256": _sparse_patterns_hash(repo_root, sparse=sparse),
        "git_index_identity": str(index_state.get("identity") or ""),
        "split_index_files": int(index_state.get("split_index_files") or 0),
        "ignore_stat": bool(view_config.get("ignore_stat")),
        "trust_ctime": bool(view_config.get("trust_ctime", True)),
        "check_stat": str(view_config.get("check_stat") or "default"),
    }
    payload["content_view_fingerprint"] = _content_view_fingerprint(payload)
    payload["view_fingerprint"] = _view_fingerprint(payload)
    return payload


def collect_repository_evidence(repo_root: Path, *, deep: bool = False) -> dict[str, Any]:
    """Collect source-snapshot provenance without contacting a remote.

    The lightweight fields are safe to refresh passively. ``deep=True`` adds
    worktree-filter, submodule, sparse-materialization, alternate-object, and
    signature-verification checks intended for explicit index/verify work.
    """
    repo_root = Path(repo_root)
    view = light_view_state(repo_root)
    if view.get("status") == "missing":
        return {"status": "missing", "assurance": "FILESYSTEM_BOUND", "repo_root": str(repo_root)}
    if view.get("status") == "non_git":
        return {
            "status": "non_git",
            "assurance": "FILESYSTEM_BOUND",
            "repo_root": _safe_resolve(repo_root),
            "view_fingerprint": str(view.get("view_fingerprint") or ""),
            "limitations": ["Git commit/tree provenance is unavailable; exact source windows are bound to current content hashes instead."],
        }
    if view.get("status") == "invalid_repo_root":
        payload = {
            "status": "invalid_repo_root",
            "assurance": "INVALID_REPOSITORY_ROOT",
            "repo_root": _safe_resolve(repo_root),
            "git_top_level": str(view.get("git_top_level") or ""),
        }
        payload["view_fingerprint"] = str(view.get("view_fingerprint") or "")
        return payload

    head = str(view.get("head_sha") or "")
    effective_rc, effective_tree_value = _run_git(repo_root, "rev-parse", "HEAD^{tree}")
    effective_tree = effective_tree_value if effective_rc == 0 else ""
    raw = _raw_commit(repo_root, head)
    format_rc, object_format_value = _run_git(repo_root, "rev-parse", "--show-object-format")
    object_format = object_format_value if format_rc == 0 else ""
    git_dir_rc, git_dir_value = _run_git(repo_root, "rev-parse", "--git-dir")
    git_dir = git_dir_value if git_dir_rc == 0 else ""
    common_rc, git_common_dir_value = _run_git(repo_root, "rev-parse", "--git-common-dir")
    git_common_dir = git_common_dir_value if common_rc == 0 else ""
    replacements = list(view.get("replace_refs") or [])
    history_view = _history_view(repo_root, git_common_dir, replace_refs_present=bool(replacements))
    status = _status_summary(repo_root)
    sparse = bool(view.get("sparse_checkout"))
    sparse_cone = bool(view.get("sparse_cone"))

    payload: dict[str, Any] = {
        "status": "git",
        "repo_root": _safe_resolve(repo_root),
        "git_top_level": _safe_resolve(repo_root),
        "object_format": object_format or "sha1",
        "head_sha": head,
        "raw_tree_sha": str(raw.get("tree_sha") or ""),
        "effective_tree_sha": effective_tree,
        "replace_refs": replacements,
        "replace_refs_present": bool(replacements),
        "replace_ref_base": str(view.get("replace_ref_base") or "refs/replace"),
        "git_no_replace_objects": bool(view.get("git_no_replace_objects")),
        "git_dir": git_dir,
        "git_common_dir": git_common_dir,
        "sparse_checkout": sparse,
        "sparse_cone": sparse_cone,
        "sparse_patterns_sha256": str(view.get("sparse_patterns_sha256") or ""),
        "git_stat_trust": {
            "ignore_stat": bool(view.get("ignore_stat")),
            "trust_ctime": bool(view.get("trust_ctime", True)),
            "check_stat": str(view.get("check_stat") or "default"),
        },
        "working_tree": status,
        "author_claim": raw.get("author_claim") or {},
        "committer_claim": raw.get("committer_claim") or {},
        "commit_signature": {
            "present": bool(raw.get("signature_present")),
            "verified": False,
            "verification_status": "not_checked",
        },
        "ignored_git_repository_environment_overrides": [
            key for key in GIT_REPOSITORY_ENV_OVERRIDES if key in os.environ
        ],
        "history_view": history_view,
    }
    anomalies: list[str] = []
    if replacements:
        anomalies.append("replace_refs_active")
    if str(payload.get("replace_ref_base") or "refs/replace") != "refs/replace":
        anomalies.append("custom_replace_ref_base")
    if payload["raw_tree_sha"] and effective_tree and payload["raw_tree_sha"] != effective_tree:
        anomalies.append("effective_tree_differs_from_raw_commit_tree")
    if sparse:
        anomalies.append("sparse_checkout_active")
    if bool(view.get("ignore_stat")):
        anomalies.append("git_ignore_stat_active")
    if not bool(view.get("trust_ctime", True)):
        anomalies.append("git_ctime_trust_disabled")
    if str(view.get("check_stat") or "default").lower() == "minimal":
        anomalies.append("git_checkstat_minimal")
    if bool(status.get("dirty")):
        anomalies.append("working_tree_dirty")
    if history_view.get("shallow_repository"):
        anomalies.append("shallow_history")
    if history_view.get("grafts_present"):
        anomalies.append("grafts_active")
    if history_view.get("partial_clone_configured"):
        anomalies.append("partial_clone_configured")

    if deep:
        filter_state = _active_configured_filters(repo_root)
        filters = list(filter_state.get("configured") or [])
        attributes = {
            "paths": list(filter_state.get("attribute_paths") or []),
            "filter_names": list(filter_state.get("referenced") or []),
        }
        submodules = _submodule_evidence(repo_root, deep=True)
        unmaterialized = _unmaterialized_tracked(repo_root)
        alternates = _alternates(repo_root, git_common_dir)
        index_flags = _index_flag_evidence(repo_root)
        deep_index_state = _git_index_state(repo_root, content_hash=True)
        payload.update({
            "git_index": deep_index_state,
            "configured_filter_drivers": filters,
            "active_filter_drivers": list(filter_state.get("active") or []),
            "configured_filter_keys": _configured_filter_keys(repo_root),
            "attribute_filters": attributes,
            "submodules": submodules,
            "unmaterialized_tracked": unmaterialized,
            "alternate_object_storage": alternates,
            "index_flags": index_flags,
        })
        if filter_state.get("active"):
            anomalies.append("worktree_content_filters_referenced")
        if submodules.get("gitlink_count"):
            anomalies.append("submodules_present")
            if not submodules.get("all_at_gitlink", False):
                anomalies.append("submodule_worktree_not_at_recorded_gitlink")
        if unmaterialized.get("count"):
            anomalies.append("tracked_paths_not_materialized")
        if alternates.get("present"):
            anomalies.append("alternate_object_storage_active")
        if int(index_flags.get("assume_unchanged_count") or 0):
            anomalies.append("assume_unchanged_index_entries")
        if int(index_flags.get("skip_worktree_count") or 0) and not sparse:
            anomalies.append("manual_skip_worktree_index_entries")
        if not bool(deep_index_state.get("stable", True)):
            anomalies.append("git_index_changed_during_verification")
        signature = payload["commit_signature"]
        if signature["present"]:
            # Do not invoke gpg/ssh signature helpers automatically: Git allows
            # those verifier programs to be configured as arbitrary local
            # executables. Presence is recorded, but identity verification is
            # intentionally a separate explicit trust operation.
            signature["verified"] = False
            signature["verification_status"] = "present_not_verified_external_verifier_not_executed"
        else:
            signature["verification_status"] = "unsigned"

    # VERIFIED_SNAPSHOT is about exact source snapshot binding, not human
    # authorship. Author/committer metadata remains an unverified claim unless
    # a signature is separately verified.
    strong_blockers = {
        "replace_refs_active",
        "custom_replace_ref_base",
        "effective_tree_differs_from_raw_commit_tree",
        "sparse_checkout_active",
        "working_tree_dirty",
        "worktree_content_filters_referenced",
        "submodules_present",
        "tracked_paths_not_materialized",
        "assume_unchanged_index_entries",
        "manual_skip_worktree_index_entries",
        "git_ignore_stat_active",
        "git_ctime_trust_disabled",
        "git_checkstat_minimal",
        "git_index_changed_during_verification",
    }
    if deep and head and payload["raw_tree_sha"] and not (set(anomalies) & strong_blockers):
        assurance = "VERIFIED_SNAPSHOT"
    else:
        # Lightweight calls deliberately do not over-claim the deep checks that
        # were materialized during indexing/explicit verification.
        assurance = "WORKING_TREE_BOUND"
    payload["assurance"] = assurance
    payload["anomalies"] = sorted(set(anomalies))
    payload["content_view_fingerprint"] = str(
        view.get("content_view_fingerprint") or _content_view_fingerprint(payload)
    )
    payload["view_fingerprint"] = str(view.get("view_fingerprint") or _view_fingerprint(payload))
    payload["limitations"] = [
        "VERIFIED_SNAPSHOT binds the exact Git root/HEAD/tree and a passively clean, anomaly-checked source view; exact source-window bytes are independently content-hash-bound.",
        "Repository provenance cannot prove that rewritten, unreachable, garbage-collected, or remote-only history never existed without an external anchor.",
        "Git author and committer fields are metadata claims, not verified human identity unless a separately checked signature establishes more.",
        "Exact source windows remain bound to their current content hashes even when repository-level assurance is reduced.",
        "VERIFIED_SNAPSHOT does not assert absence or content of Git-ignored untracked files; normal repository scope honors .gitignore and forensic include_ignored search is explicit.",
    ]
    return payload


def light_view_fingerprint(
    repo_root: Path,
    *,
    known_head: str = "",
    exact_root_verified: bool = False,
) -> str:
    return str(light_view_state(
        repo_root,
        known_head=known_head,
        exact_root_verified=exact_root_verified,
    ).get("view_fingerprint") or "")


def _git_file_identity(repo_root: Path, rel_path: str) -> dict[str, Any]:
    rel = Path(rel_path).as_posix()
    literal = f":(literal){rel}"
    stage_rc, stage = _run_git(repo_root, "ls-files", "--stage", "--", literal)
    tracked = stage_rc == 0 and bool(stage.strip())
    index_blob = ""
    if tracked:
        first = stage.splitlines()[0].split()
        if len(first) >= 2:
            index_blob = first[1]
    tree_rc, tree_raw = _run_git_bytes(repo_root, "ls-tree", "-z", "HEAD", "--", literal, no_replace=True)
    head_blob = ""
    if tree_rc == 0 and tree_raw:
        left, sep, _ = tree_raw.split(b"\0", 1)[0].partition(b"\t")
        if sep:
            parts = left.decode("ascii", errors="ignore").split()
            if len(parts) >= 3 and parts[1] == "blob":
                head_blob = parts[2]
    hash_rc, worktree_oid = _run_git(repo_root, "hash-object", "--no-filters", "--", rel)
    literal_matches_index = bool(index_blob and hash_rc == 0 and index_blob == worktree_oid)
    index_matches_head = bool(index_blob and head_blob and index_blob == head_blob)
    return {
        "tracked": tracked,
        "index_blob_oid": index_blob,
        "head_blob_oid": head_blob,
        "working_tree_blob_oid": worktree_oid if hash_rc == 0 else "",
        "literal_worktree_matches_head_blob": bool(head_blob and hash_rc == 0 and head_blob == worktree_oid),
        "literal_worktree_matches_index_blob": literal_matches_index,
        "index_matches_head_blob": index_matches_head,
        # This deliberately compares literal bytes without invoking Git clean
        # filters. A filtered worktree may therefore be reported as different,
        # which lowers assurance rather than executing a configured helper.
        "path_dirty": bool(not tracked or not literal_matches_index or not index_matches_head),
    }


def encode_evidence(payload: dict[str, Any]) -> str:
    """Encode compact source evidence; v4 binds repository identity."""
    assurance_codes = {"VERIFIED_SNAPSHOT": "V", "WORKING_TREE_BOUND": "W", "FILESYSTEM_BOUND": "F"}
    wire = [
        EVIDENCE_TOKEN_VERSION, assurance_codes.get(str(payload.get("assurance") or ""), "F"),
        str(payload.get("project_id") or ""), str(payload.get("repo_id") or ""),
        str(payload.get("commit_sha") or ""), str(payload.get("raw_tree_sha") or ""),
        str(payload.get("view_fingerprint") or ""), str(payload.get("path") or ""),
        str(payload.get("source_sha256") or ""), str(payload.get("git_blob_oid") or ""),
        int(payload.get("start_line") or 0), int(payload.get("end_line") or 0),
    ]
    raw = json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_EVIDENCE_PAYLOAD_BYTES:
        raise ValueError("evidence payload is too large")
    compressed = zlib.compress(raw, level=9)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    checksum = _sha(raw)[:16]
    return f"ev{EVIDENCE_TOKEN_VERSION}z.{encoded}.{checksum}"


def decode_evidence(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > MAX_EVIDENCE_TOKEN_CHARS:
        raise ValueError("evidence id is too large")
    version = (
        5 if token.startswith("ev5z.")
        else 4 if token.startswith("ev4z.")
        else 3 if token.startswith("ev3z.")
        else 0
    )
    if not version:
        raise ValueError("unsupported evidence id version")
    prefix = f"ev{version}z."
    try:
        encoded, checksum = token[len(prefix):].rsplit(".", 1)
        compressed = base64.urlsafe_b64decode((encoded + "=" * (-len(encoded) % 4)).encode("ascii"))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_EVIDENCE_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_PAYLOAD_BYTES or decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("evidence payload exceeds safe decompression bounds")
        if _sha(raw)[:16] != checksum:
            raise ValueError("evidence id checksum mismatch")
        wire = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"invalid evidence id: {exc}") from exc
    expected_len = 12 if version == 4 else 11 if version == 3 else 11
    if not isinstance(wire, list) or len(wire) != expected_len or int(wire[0] or 0) != version:
        raise ValueError("unsupported evidence payload version")
    assurance_names = {
        "V": "VERIFIED_SNAPSHOT",
        "W": "WORKING_TREE_BOUND",
        "F": "FILESYSTEM_BOUND",
        "C": "CONTENT_MANIFEST_BOUND",
    }
    if version == 5:
        source_id, source_type, revision_key, content_identity, path, source_sha256, start_line, end_line, project_id = wire[2:]
        return {
            "v": version,
            "assurance": assurance_names.get(str(wire[1]), "CONTENT_MANIFEST_BOUND"),
            "authenticity": "self_contained_checksum_not_signature",
            "project_id": str(project_id),
            "repo_id": "",
            "source_id": str(source_id),
            "source_type": str(source_type),
            "revision_key": str(revision_key),
            "content_identity": str(content_identity),
            "path": str(path),
            "source_sha256": str(source_sha256),
            "start_line": int(start_line),
            "end_line": int(end_line),
        }
    if version == 4:
        project_id, repo_id, commit_sha, raw_tree_sha, view_fingerprint, path, source_sha256, git_blob_oid, start_line, end_line = wire[2:]
    else:
        project_id, commit_sha, raw_tree_sha, view_fingerprint, path, source_sha256, git_blob_oid, start_line, end_line = wire[2:]
        repo_id = ""
    return {
        "v": version, "assurance": assurance_names.get(str(wire[1]), "FILESYSTEM_BOUND"),
        "authenticity": "self_contained_checksum_not_signature", "project_id": str(project_id),
        "repo_id": str(repo_id), "commit_sha": str(commit_sha), "raw_tree_sha": str(raw_tree_sha),
        "view_fingerprint": str(view_fingerprint), "path": str(path), "source_sha256": str(source_sha256),
        "git_blob_oid": str(git_blob_oid), "start_line": int(start_line), "end_line": int(end_line),
    }


def encode_corpus_evidence(payload: dict[str, Any]) -> str:
    """Encode a compact evidence token for a content-manifest source revision."""
    wire = [
        CORPUS_EVIDENCE_TOKEN_VERSION,
        "C",
        str(payload.get("source_id") or ""),
        str(payload.get("source_type") or "directory"),
        str(payload.get("revision_key") or ""),
        str(payload.get("content_identity") or ""),
        str(payload.get("path") or ""),
        str(payload.get("source_sha256") or ""),
        int(payload.get("start_line") or 0),
        int(payload.get("end_line") or 0),
        str(payload.get("project_id") or ""),
    ]
    raw = json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_EVIDENCE_PAYLOAD_BYTES:
        raise ValueError("evidence payload is too large")
    compressed = zlib.compress(raw, level=9)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    checksum = _sha(raw)[:16]
    return f"ev{CORPUS_EVIDENCE_TOKEN_VERSION}z.{encoded}.{checksum}"


def build_corpus_source_evidence(
    *,
    project_id: str,
    source_id: str,
    source_type: str,
    revision_key: str,
    content_identity: str,
    rel_path: str,
    source_sha256: str,
    start_line: int,
    end_line: int,
) -> dict[str, Any]:
    payload = {
        "project_id": project_id,
        "source_id": source_id,
        "source_type": source_type,
        "revision_key": revision_key,
        "content_identity": content_identity,
        "path": Path(rel_path).as_posix(),
        "source_sha256": source_sha256,
        "start_line": int(start_line),
        "end_line": int(end_line),
    }
    return {
        "evidence_id": encode_corpus_evidence(payload),
        "assurance": "CONTENT_MANIFEST_BOUND",
        "authenticity": "self_contained_checksum_not_signature",
        "source_id": source_id,
        "source_type": source_type,
        "revision_key": revision_key,
        "content_identity": content_identity,
        "range": {"start_line": int(start_line), "end_line": int(end_line)},
    }


def build_source_evidence(
    *,
    repo_root: Path,
    project_id: str,
    repo_id: str,
    branch_key: str,
    commit_sha: str,
    rel_path: str,
    source_sha256: str,
    indexed_sha256: str,
    start_line: int,
    end_line: int,
    assurance_hint: str = "",
    view_hint: dict[str, Any] | None = None,
    snapshot_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repository = dict(view_hint or light_view_state(repo_root, known_head=commit_sha))
    snapshot = dict(snapshot_hint or {})
    assurance = assurance_hint or (
        "FILESYSTEM_BOUND" if repository.get("status") != "git" else "WORKING_TREE_BOUND"
    )
    raw_tree_sha = ""
    if repository.get("status") == "git":
        if (
            snapshot
            and str(snapshot.get("view_fingerprint") or "") == str(repository.get("view_fingerprint") or "")
            and str(snapshot.get("head_sha") or "") == str(commit_sha or "")
        ):
            raw_tree_sha = str(snapshot.get("raw_tree_sha") or "")
        if not raw_tree_sha:
            raw_tree_sha = str(_raw_commit(repo_root, commit_sha).get("tree_sha") or "")
    git_file = _git_file_identity(repo_root, rel_path) if repository.get("status") == "git" else {
        "tracked": False,
        "index_blob_oid": "",
        "head_blob_oid": "",
        "working_tree_blob_oid": "",
        "literal_worktree_matches_head_blob": False,
        "path_dirty": True,
    }
    payload = {
        "v": EVIDENCE_TOKEN_VERSION,
        "assurance": assurance,
        "project_id": project_id,
        "repo_id": repo_id,
        "commit_sha": commit_sha,
        "raw_tree_sha": raw_tree_sha,
        "view_fingerprint": str(repository.get("view_fingerprint") or ""),
        "path": Path(rel_path).as_posix(),
        "source_sha256": source_sha256,
        "git_blob_oid": str(git_file.get("head_blob_oid") or ""),
        "start_line": int(start_line),
        "end_line": int(end_line),
    }
    token = encode_evidence(payload)
    return {
        "evidence_id": token,
        "assurance": assurance,
        "authenticity": "self_contained_checksum_not_signature",
        "git_blob_oid": str(git_file.get("head_blob_oid") or ""),
        "literal_worktree_matches_head_blob": bool(git_file.get("literal_worktree_matches_head_blob")),
        "range": {"start_line": int(start_line), "end_line": int(end_line)},
    }


def verify_source_evidence(repo_root: Path, token: str) -> dict[str, Any]:
    payload = decode_evidence(token)
    rel = str(payload.get("path") or "")
    candidate = Path(rel)
    if not rel or candidate.is_absolute() or ".." in candidate.parts:
        return {"verdict": "INVALID_EVIDENCE", "reason": "evidence path is not safe", "payload": payload}
    path = repo_root / candidate
    allowed, policy_reason = indexing_policy.source_evidence_path_allowed(path, repo_root=repo_root)
    if not allowed:
        return {
            "verdict": "INVALID_EVIDENCE_POLICY",
            "reason": f"evidence path is not eligible for exact source evidence: {policy_reason}",
            "path": rel,
            "payload": payload,
        }
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("source is not a regular file")
        stat = path.stat()
        if stat.st_size > MAX_EVIDENCE_VERIFY_FILE_BYTES:
            return {
                "verdict": "VERIFICATION_BUDGET_EXCEEDED",
                "reason": f"source exceeds {MAX_EVIDENCE_VERIFY_FILE_BYTES} byte evidence verification limit",
                "payload": payload,
            }
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        current_sha = digest.hexdigest()
    except OSError as exc:
        return {"verdict": "STALE_SOURCE", "reason": str(exc), "payload": payload}
    expected_assurance = str(payload.get("assurance") or "FILESYSTEM_BOUND")
    # Explicit verification may spend the extra Git work required to decide
    # whether a previously VERIFIED_SNAPSHOT is still clean and anomaly-free.
    current_repo = collect_repository_evidence(repo_root, deep=(expected_assurance == "VERIFIED_SNAPSHOT"))
    source_current = current_sha == str(payload.get("source_sha256") or "")
    repository_identity_current = (
        str(current_repo.get("head_sha") or "") == str(payload.get("commit_sha") or "")
        and str(current_repo.get("raw_tree_sha") or "") == str(payload.get("raw_tree_sha") or "")
        and str(current_repo.get("view_fingerprint") or "") == str(payload.get("view_fingerprint") or "")
    )
    current_assurance = str(current_repo.get("assurance") or "FILESYSTEM_BOUND")
    current_git_file = _git_file_identity(repo_root, rel) if current_repo.get("status") == "git" else {}
    expected_blob = str(payload.get("git_blob_oid") or "")
    current_blob = str(current_git_file.get("head_blob_oid") or "")
    snapshot_current: bool | None
    if expected_assurance == "FILESYSTEM_BOUND":
        snapshot_current = None
    elif expected_assurance == "VERIFIED_SNAPSHOT":
        snapshot_current = bool(repository_identity_current and current_assurance == "VERIFIED_SNAPSHOT")
    else:
        # WORKING_TREE_BOUND evidence only binds the named source bytes plus
        # the Git view identity. It does not claim the entire dirty worktree
        # was snapshotted.
        snapshot_current = False

    if not source_current:
        verdict = "STALE_SOURCE"
        reason = "current source bytes differ from the evidence id"
    elif expected_assurance == "FILESYSTEM_BOUND":
        verdict = "CURRENT_SOURCE_FILESYSTEM_BOUND"
        reason = "source bytes still match; no Git snapshot identity was available when evidence was captured"
    elif expected_assurance == "WORKING_TREE_BOUND" and repository_identity_current:
        verdict = "CURRENT_SOURCE_WORKING_TREE_BOUND"
        reason = "source bytes and Git view identity match; evidence did not claim a fully verified clean snapshot"
    elif expected_assurance == "VERIFIED_SNAPSHOT" and snapshot_current:
        verdict = "CURRENT_VERIFIED_SNAPSHOT"
        reason = "source bytes and the verified clean Git snapshot/view match the evidence id"
    elif source_current:
        verdict = "SOURCE_CURRENT_SNAPSHOT_CHANGED"
        reason = "source bytes still match, but repository snapshot/view assurance or identity changed"
    return {
        "verdict": verdict,
        "reason": reason,
        "current": source_current and verdict.startswith("CURRENT_"),
        "source_current": source_current,
        "snapshot_current": snapshot_current,
        "repository_identity_current": repository_identity_current,
        "path": rel,
        "evidence_assurance": expected_assurance,
        "evidence_authenticity": str(payload.get("authenticity") or "unknown"),
        "expected_source_sha256": payload.get("source_sha256", ""),
        "current_source_sha256": current_sha,
        "expected_commit_sha": payload.get("commit_sha", ""),
        "current_commit_sha": current_repo.get("head_sha", ""),
        "expected_tree_sha": payload.get("raw_tree_sha", ""),
        "current_tree_sha": current_repo.get("raw_tree_sha", ""),
        "expected_git_blob_oid": expected_blob,
        "current_git_blob_oid": current_blob,
        "git_blob_current": (expected_blob == current_blob) if expected_blob else None,
        "current_assurance": current_assurance,
        "range": {"start_line": payload.get("start_line"), "end_line": payload.get("end_line")},
    }


def repository_scope_constraints(repo_root: Path) -> dict[str, Any]:
    """Return cheap repository-layout boundaries relevant to exhaustive search.

    Normal worktrees avoid a full tracked-file existence scan. Sparse worktrees
    pay that cost because absent tracked paths otherwise create a false
    repository-completeness claim.
    """
    light = light_view_state(repo_root)
    if light.get("status") != "git":
        return {
            "sparse_checkout": False,
            "unmaterialized_tracked_file_count": 0,
            "unmaterialized_tracked_sample": [],
            "submodule_gitlink_count": 0,
            "submodule_repositories_scanned": False,
        }
    missing = {"count": 0, "sample": [], "verified": False}
    if light.get("sparse_checkout"):
        missing = _unmaterialized_tracked(repo_root)
    submodules = _submodule_evidence(repo_root, deep=False)
    return {
        "sparse_checkout": bool(light.get("sparse_checkout")),
        "unmaterialized_tracked_file_count": int(missing.get("count") or 0),
        "unmaterialized_tracked_sample": list(missing.get("sample") or []),
        "submodule_gitlink_count": int(submodules.get("gitlink_count") or 0),
        "submodule_gitlinks": list(submodules.get("gitlinks") or []),
        "submodule_repositories_scanned": False,
    }
