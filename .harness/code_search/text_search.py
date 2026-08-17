from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import indexing_policy
import project_workspace
import safety

from . import engine, provenance, store

CURSOR_VERSION = 4
DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 5000
DEFAULT_PREVIEW_CHARS = 320
MAX_PREVIEW_CHARS = 4096
MAX_PATTERN_BYTES = 4096
MAX_ARG_BYTES = 48_000
MAX_FILES_PER_SHARD = 256
MAX_INVENTORY_FILES = 1000
DEFAULT_OPERATION_TIMEOUT_SECONDS = 20.0
MAX_OPERATION_TIMEOUT_SECONDS = 45.0
OPERATION_DEADLINE_RESERVE_SECONDS = 0.35
SEARCH_CACHE_SCHEMA_VERSION = 4
SEARCH_CACHE_TTL_SECONDS = 6 * 60 * 60
SEARCH_CACHE_MAX_RUNS = 24


class TextSearchError(RuntimeError):
    pass


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="replace")
    return hashlib.sha256(value).hexdigest()


def _safe_int(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _normalize_scope_paths(paths: Iterable[str] | None) -> tuple[list[str], str | None]:
    normalized: list[str] = []
    for raw in paths or []:
        value = str(raw or "").strip().replace("\\", "/")
        if not value or value == ".":
            value = ""
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            return [], f"search paths must be repository-relative and may not traverse upward: {raw!r}"
        clean = candidate.as_posix().strip("/") if value else ""
        if clean not in normalized:
            normalized.append(clean)
    normalized.sort()
    return normalized, None


def _in_scope(repo_relative: str, scopes: list[str]) -> bool:
    if not scopes or scopes == [""]:
        return True
    rel = repo_relative.replace("\\", "/").strip("/")
    return any(not scope or rel == scope or rel.startswith(scope.rstrip("/") + "/") for scope in scopes)


def _scope_probe_hash(rows: list[dict[str, Any]]) -> str:
    values = [
        "|".join([
            str(row.get("repo_relative") or ""),
            "1" if row.get("included") else "0",
            str(row.get("reason") or ""),
            str(row.get("content_hash") or ""),
            str(row.get("size_bytes") or 0),
        ])
        for row in rows
    ]
    return _sha("\n".join(sorted(values)))


def _fingerprint(
    *,
    pattern: str,
    scopes: list[str],
    ignore_case: bool,
    fixed_string: bool,
    include_ignored: bool,
    preview_chars: int,
    source_probe_hash: str,
    source_token: str,
    branch_key: str,
) -> str:
    payload = {
        "pattern": pattern,
        "paths": scopes,
        "ignore_case": bool(ignore_case),
        "fixed_string": bool(fixed_string),
        "preview_chars": int(preview_chars),
        "source_probe_hash": source_probe_hash,
        "source_token": source_token,
        "branch_key": branch_key,
        "include_ignored": bool(include_ignored),
    }
    return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _encode_cursor(offset: int, fingerprint: str, search_id: str) -> str:
    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "offset": int(offset),
            "fingerprint": fingerprint,
            "search_id": search_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str, str]:
    if not cursor:
        return 0, "", ""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(payload.get("v")) != CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        offset = int(payload.get("offset"))
        fingerprint = str(payload.get("fingerprint") or "")
        search_id = str(payload.get("search_id") or "")
        if offset < 0 or not fingerprint or not search_id:
            raise ValueError("invalid cursor payload")
        return offset, fingerprint, search_id
    except Exception as exc:
        raise ValueError(f"invalid cursor: {exc}") from exc


def _run_git_bytes(repo_root: Path, *args: str, timeout: float = 10.0) -> tuple[int, bytes]:
    env = provenance.sanitized_git_environment()
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo_root), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return completed.returncode, completed.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, b""


def _quick_source_token(repo_root: Path, branch: Any, *, include_ignored: bool = False, trusted_snapshot: bool = False) -> str:
    """Cheaply bind a materialized search to the current repository snapshot.

    Clean Git trees are fully identified by commit SHA. Dirty Git trees hash the
    changed/untracked path set plus the bytes of reasonably sized changed files,
    so repeated pages do not need to re-read the complete repository. Non-Git
    trees deliberately return an empty token and fall back to a full policy probe.
    """
    if str(getattr(branch, "source", "")) not in {"git_branch", "git_detached", "git_unknown"}:
        return ""
    commit = str(getattr(branch, "commit_sha", "") or "")
    dirty = bool(getattr(branch, "dirty", False))
    view_fingerprint = provenance.light_view_fingerprint(
        repo_root,
        known_head=commit,
        exact_root_verified=True,
    )
    if not dirty:
        if trusted_snapshot and not include_ignored:
            return _sha(f"git-clean\0{commit}\0{view_fingerprint}")
        # A clean-looking Git status is not a sufficient source token for a
        # repository that was indexed under reduced assurance (for example
        # assume-unchanged/skip-worktree/filter-backed state). Recompute the
        # raw policy/content probe instead of allowing stale materialized pages.
        return ""

    # Worktree diffs may invoke clean/process filters. If a configured driver
    # is actually referenced by attributes and the literal worktree is dirty,
    # fall back to Awoki's raw filesystem policy probe instead of executing it.
    if dirty and provenance.active_configured_filter_names(repo_root):
        return ""

    if dirty:
        diff_rc, diff_raw = _run_git_bytes(repo_root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", timeout=12.0)
        other_rc, other_raw = _run_git_bytes(repo_root, "ls-files", "--others", "--exclude-standard", "-z", timeout=12.0)
    else:
        diff_rc, diff_raw = 0, b""
        other_rc, other_raw = 0, b""
    ignored_raw = b""
    ignored_rc = 0
    if include_ignored:
        ignored_rc, ignored_raw = _run_git_bytes(
            repo_root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", timeout=12.0
        )
    if diff_rc != 0 or other_rc != 0 or ignored_rc != 0:
        return ""
    names = sorted({
        os.fsdecode(raw)
        for raw in (diff_raw + other_raw + ignored_raw).split(b"\0")
        if raw
    })
    digest = hashlib.sha256()
    digest.update((f"git-forensic\0{commit}\0{view_fingerprint}\0" if include_ignored else f"git-dirty\0{commit}\0{view_fingerprint}\0").encode("utf-8"))
    for rel_text in names:
        rel = Path(rel_text)
        digest.update(rel.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        target = repo_root / rel
        try:
            stat = target.lstat()
        except OSError:
            digest.update(b"missing\0")
            continue
        digest.update(f"{stat.st_mode}\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode("ascii"))
        if target.is_symlink():
            try:
                digest.update(os.readlink(target).encode("utf-8", errors="surrogateescape"))
            except OSError:
                digest.update(b"unreadable-symlink")
            digest.update(b"\0")
            continue
        if target.is_file():
            # Dirty changed files are part of the source snapshot regardless of
            # size. Stream the hash so a same-size edit to a large lexical-only
            # source cannot reuse a stale materialized-search cursor.
            try:
                with target.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                digest.update(b"unreadable")
        digest.update(b"\0")
    return digest.hexdigest()


def _lexical_partition(
    structural_included: list[dict[str, Any]],
    structural_excluded: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in structural_included + structural_excluded:
        row = dict(raw)
        lexical = bool(row.get("lexical_included", row.get("included")))
        row["included"] = lexical
        if lexical:
            row["reason"] = str(row.get("policy_reason") or row.get("reason") or "source_text_allowlist")
            included.append(row)
        else:
            excluded.append(row)
    return included, excluded


def _eligibility_snapshot(
    paths: Any,
    project_id: str,
    repo_root: Path,
    branch: Any,
    *,
    include_ignored: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    """Return policy eligibility, reusing a clean same-commit index manifest when safe."""
    pp = project_workspace.paths_for(paths.root, project_id)
    legacy = str(getattr(branch, "repo_id", "")) == f"{project_id}:repo"
    repo_name = "default" if legacy else str(getattr(branch, "repo_id", "")).split(":", 1)[-1]
    manifest = indexing_policy.read_index_manifest(engine._repo_manifest_path(pp.project_dir, repo_name, legacy=legacy))
    manifest_matches = bool(
        manifest
        and not bool(getattr(branch, "dirty", True))
        and manifest.get("project_id") == project_id
        and int(manifest.get("index_policy_version") or 0) == indexing_policy.INDEX_POLICY_VERSION
        and str(manifest.get("engine_version") or "") == engine.ENGINE_VERSION
        and manifest.get("branch_key") == getattr(branch, "branch_key", "")
        and str(manifest.get("commit_sha") or "") == str(getattr(branch, "commit_sha", "") or "")
        and str((manifest.get("repository_evidence") or {}).get("assurance") or "") == "VERIFIED_SNAPSHOT"
        and str(manifest.get("repository_view_fingerprint") or "") == provenance.light_view_fingerprint(
            repo_root,
            known_head=str(getattr(branch, "commit_sha", "") or ""),
            exact_root_verified=True,
        )
        and not bool(manifest.get("dirty"))
        and isinstance(manifest.get("included"), list)
        and isinstance(manifest.get("excluded"), list)
    )
    # The normal structural manifest intentionally honors .gitignore. A forensic
    # include_ignored lexical search must enumerate live rather than reusing that
    # narrower manifest.
    if manifest_matches and not include_ignored:
        structural_included = [dict(row) for row in manifest.get("included") or [] if isinstance(row, dict)]
        structural_excluded = [dict(row) for row in manifest.get("excluded") or [] if isinstance(row, dict)]
        included, excluded = _lexical_partition(structural_included, structural_excluded)
        return included, excluded, str(manifest.get("source_probe_hash") or ""), "index_manifest"
    structural_included, structural_excluded, source_probe_hash = engine._scan_repository(
        paths, project_id, repo_root, include_ignored=include_ignored
    )
    included, excluded = _lexical_partition(structural_included, structural_excluded)
    return included, excluded, source_probe_hash, "live_policy_scan"


def _source_exclusion_summary(repo_root: Path, excluded: list[dict[str, Any]]) -> tuple[int, Counter[str]]:
    """Count policy-excluded repository text/source candidates conservatively."""
    reasons: Counter[str] = Counter()
    count = 0
    for row in excluded:
        rel = str(row.get("repo_relative") or "")
        if not rel:
            continue
        candidate = row.get("repository_source_candidate")
        if candidate is None:
            candidate = engine._is_source_like(repo_root / rel)
        if not bool(candidate):
            continue
        count += 1
        reasons[str(row.get("reason") or "unknown")] += 1
    return count, reasons


def _cache_path(project_dir: Path) -> Path:
    return project_dir / "index" / "sqlite" / "text-search-cache.sqlite"


def _cache_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_cache(path: Path) -> None:
    with closing(_cache_connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                search_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                project_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                source_token TEXT NOT NULL,
                source_probe_hash TEXT NOT NULL,
                branch_json TEXT NOT NULL,
                pattern TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                ignore_case INTEGER NOT NULL,
                fixed_string INTEGER NOT NULL,
                include_ignored INTEGER NOT NULL DEFAULT 0,
                preview_chars INTEGER NOT NULL,
                eligible_file_count INTEGER NOT NULL,
                repository_source_file_count INTEGER NOT NULL DEFAULT 0,
                policy_excluded_file_count INTEGER NOT NULL,
                policy_excluded_reasons_json TEXT NOT NULL,
                policy_excluded_source_file_count INTEGER NOT NULL DEFAULT 0,
                policy_excluded_source_reasons_json TEXT NOT NULL DEFAULT '{}',
                eligibility_source TEXT NOT NULL,
                timed_out_files_json TEXT NOT NULL DEFAULT '[]',
                error_status TEXT NOT NULL DEFAULT '',
                error_reason TEXT NOT NULL DEFAULT '',
                scan_duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_files (
                search_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(search_id, path),
                UNIQUE(search_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS search_files_pending_idx
                ON search_files(search_id, status, ordinal);
            CREATE TABLE IF NOT EXISTS search_matches (
                search_id TEXT NOT NULL,
                match_index INTEGER NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                column_number INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                match_bytes INTEGER,
                match_preview TEXT NOT NULL,
                preview_truncated INTEGER NOT NULL,
                PRIMARY KEY(search_id, match_index)
            );
            CREATE INDEX IF NOT EXISTS search_matches_path_idx
                ON search_matches(search_id, path);
            """
        )
        existing_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(search_runs)").fetchall()}
        migrations = {
            "repository_source_file_count": "INTEGER NOT NULL DEFAULT 0",
            "policy_excluded_source_file_count": "INTEGER NOT NULL DEFAULT 0",
            "policy_excluded_source_reasons_json": "TEXT NOT NULL DEFAULT '{}'",
            "include_ignored": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in migrations.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE search_runs ADD COLUMN {column} {ddl}")
        conn.commit()


@contextmanager
def _cache_lock(cache_path: Path):
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prune_cache(cache_path: Path) -> None:
    now = time.time()
    with closing(_cache_connect(cache_path)) as conn, conn:
        stale = conn.execute(
            "SELECT search_id FROM search_runs WHERE updated_at < ?",
            (now - SEARCH_CACHE_TTL_SECONDS,),
        ).fetchall()
        rows = conn.execute(
            "SELECT search_id FROM search_runs ORDER BY updated_at DESC"
        ).fetchall()
        overflow = rows[SEARCH_CACHE_MAX_RUNS:]
        doomed = {str(row["search_id"]) for row in stale + overflow}
        for search_id in doomed:
            conn.execute("DELETE FROM search_matches WHERE search_id=?", (search_id,))
            conn.execute("DELETE FROM search_files WHERE search_id=?", (search_id,))
            conn.execute("DELETE FROM search_runs WHERE search_id=?", (search_id,))


def _load_run(cache_path: Path, search_id: str) -> dict[str, Any] | None:
    with closing(_cache_connect(cache_path)) as conn:
        row = conn.execute("SELECT * FROM search_runs WHERE search_id=?", (search_id,)).fetchone()
        return dict(row) if row else None


def _initialize_run(
    cache_path: Path,
    *,
    search_id: str,
    project_id: str,
    fingerprint: str,
    source_token: str,
    source_probe_hash: str,
    branch: Any,
    pattern: str,
    scopes: list[str],
    ignore_case: bool,
    fixed_string: bool,
    include_ignored: bool,
    preview_chars: int,
    files: list[str],
    excluded_reasons: Counter[str],
    excluded_count: int,
    excluded_source_reasons: Counter[str],
    excluded_source_count: int,
    eligibility_source: str,
) -> None:
    now = time.time()
    with closing(_cache_connect(cache_path)) as conn, conn:
        existing = conn.execute("SELECT fingerprint FROM search_runs WHERE search_id=?", (search_id,)).fetchone()
        if existing:
            conn.execute("UPDATE search_runs SET updated_at=? WHERE search_id=?", (now, search_id))
            return
        conn.execute(
            """
            INSERT INTO search_runs(
                search_id, schema_version, project_id, fingerprint, source_token, source_probe_hash,
                branch_json, pattern, scopes_json, ignore_case, fixed_string, include_ignored, preview_chars,
                eligible_file_count, repository_source_file_count,
                policy_excluded_file_count, policy_excluded_reasons_json,
                policy_excluded_source_file_count, policy_excluded_source_reasons_json,
                eligibility_source, timed_out_files_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                search_id, SEARCH_CACHE_SCHEMA_VERSION, project_id, fingerprint, source_token,
                source_probe_hash, json.dumps(branch.__dict__, sort_keys=True), pattern,
                json.dumps(scopes, sort_keys=True), int(ignore_case), int(fixed_string), int(include_ignored), int(preview_chars),
                len(files), len(files) + int(excluded_source_count),
                int(excluded_count), json.dumps(dict(sorted(excluded_reasons.items())), sort_keys=True),
                int(excluded_source_count), json.dumps(dict(sorted(excluded_source_reasons.items())), sort_keys=True),
                eligibility_source, "[]", now, now,
            ),
        )
        conn.executemany(
            "INSERT INTO search_files(search_id, ordinal, path, status) VALUES(?,?,?,'pending')",
            [(search_id, index, path) for index, path in enumerate(files)],
        )


def _chunk_files(files: list[str]) -> Iterable[list[str]]:
    chunk: list[str] = []
    used = 0
    for path in files:
        encoded = len(os.fsencode(path)) + 1
        if chunk and (len(chunk) >= MAX_FILES_PER_SHARD or used + encoded > MAX_ARG_BYTES):
            yield chunk
            chunk = []
            used = 0
        chunk.append(path)
        used += encoded
    if chunk:
        yield chunk


def _parse_rg_output(raw: bytes) -> Iterable[tuple[str, int, int, int, bytes]]:
    """Parse `rg --null --with-filename` records without assuming safe filenames."""
    buffer = bytearray(raw)
    while buffer:
        nul = buffer.find(b"\0")
        if nul < 0:
            raise TextSearchError("ripgrep output ended before filename terminator")
        newline = buffer.find(b"\n", nul + 1)
        if newline < 0:
            raise TextSearchError("ripgrep output ended before match record terminator")
        raw_path = bytes(buffer[:nul])
        body = bytes(buffer[nul + 1 : newline])
        del buffer[: newline + 1]
        fields = body.split(b":", 3)
        if len(fields) != 4:
            raise TextSearchError("unexpected ripgrep match record shape")
        try:
            line_number = int(fields[0])
            column = int(fields[1])
            byte_offset = int(fields[2])
        except ValueError as exc:
            raise TextSearchError("unexpected ripgrep numeric match metadata") from exc
        yield os.fsdecode(raw_path), line_number, column, byte_offset, fields[3]


def _run_rg_shard(rg: str, pattern: str, files: list[str], *, cwd: Path, preview_chars: int, ignore_case: bool, fixed_string: bool, timeout_seconds: float) -> tuple[list[tuple[str, int, int, int, bytes]], bool, str]:
    args = [
        rg, "--no-messages", "--color", "never", "--text", "--sort", "path",
        "--with-filename", "--null", "--line-number", "--column", "--byte-offset",
        "--only-matching", "--max-columns", str(preview_chars),
    ]
    if ignore_case:
        args.append("--ignore-case")
    if fixed_string:
        args.append("--fixed-strings")
    args.extend(["--", pattern, *files])
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds if timeout_seconds > 0 else None,
            check=False,
            env=provenance.sanitized_git_environment(),
        )
    except subprocess.TimeoutExpired:
        return [], True, ""
    except OSError as exc:
        raise TextSearchError(f"ripgrep execution failed: {exc}") from exc
    if completed.returncode not in {0, 1}:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        return [], False, message or f"ripgrep exited with status {completed.returncode}"
    return list(_parse_rg_output(completed.stdout)), False, ""


def _search_shard_resilient(rg: str, pattern: str, files: list[str], *, cwd: Path, preview_chars: int, ignore_case: bool, fixed_string: bool, timeout_seconds: float) -> tuple[list[tuple[str, int, int, int, bytes]], list[str], str]:
    """Retry timed-out shards by splitting until an individual file boundary."""
    rows, timed_out, error = _run_rg_shard(
        rg, pattern, files, cwd=cwd, preview_chars=preview_chars,
        ignore_case=ignore_case, fixed_string=fixed_string, timeout_seconds=timeout_seconds,
    )
    if error or not timed_out:
        return rows, [], error
    if len(files) == 1:
        return [], [files[0]], ""
    middle = max(1, len(files) // 2)
    left_rows, left_timeouts, left_error = _search_shard_resilient(
        rg, pattern, files[:middle], cwd=cwd, preview_chars=preview_chars,
        ignore_case=ignore_case, fixed_string=fixed_string, timeout_seconds=timeout_seconds,
    )
    if left_error:
        return [], [], left_error
    right_rows, right_timeouts, right_error = _search_shard_resilient(
        rg, pattern, files[middle:], cwd=cwd, preview_chars=preview_chars,
        ignore_case=ignore_case, fixed_string=fixed_string, timeout_seconds=timeout_seconds,
    )
    if right_error:
        return [], [], right_error
    return left_rows + right_rows, left_timeouts + right_timeouts, ""


def _search_shard_with_deadline(
    rg: str,
    pattern: str,
    files: list[str],
    *,
    cwd: Path,
    preview_chars: int,
    ignore_case: bool,
    fixed_string: bool,
    shard_timeout_seconds: float,
    deadline: float | None,
) -> tuple[list[tuple[str, int, int, int, bytes]], list[str], list[str], str]:
    """Search one ordered shard without allowing a whole MCP request to run unbounded.

    Returns rows, terminal per-file timeouts, deferred files, and an error. Deferred
    files were not proven complete before the operation deadline and remain pending
    for a later call. Successfully searched files are always a prefix of `files`,
    preserving deterministic global result ordering across resumptions.
    """
    if not files:
        return [], [], [], ""
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= OPERATION_DEADLINE_RESERVE_SECONDS:
        return [], [], list(files), ""

    requested = max(0.0, float(shard_timeout_seconds))
    if requested <= 0:
        requested = float("inf")
    available = requested
    budget_limited = False
    if remaining is not None:
        available = min(available, max(0.05, remaining - OPERATION_DEADLINE_RESERVE_SECONDS))
        budget_limited = available + 1e-9 < requested

    rows, timed_out, error = _run_rg_shard(
        rg,
        pattern,
        files,
        cwd=cwd,
        preview_chars=preview_chars,
        ignore_case=ignore_case,
        fixed_string=fixed_string,
        timeout_seconds=0.0 if available == float("inf") else available,
    )
    if error or not timed_out:
        return rows, [], [], error
    if budget_limited:
        return [], [], list(files), ""
    if len(files) == 1:
        return [], [files[0]], [], ""

    middle = max(1, len(files) // 2)
    left_rows, left_timeouts, left_deferred, left_error = _search_shard_with_deadline(
        rg,
        pattern,
        files[:middle],
        cwd=cwd,
        preview_chars=preview_chars,
        ignore_case=ignore_case,
        fixed_string=fixed_string,
        shard_timeout_seconds=shard_timeout_seconds,
        deadline=deadline,
    )
    if left_error:
        return [], [], [], left_error
    if left_deferred:
        return left_rows, left_timeouts, left_deferred + files[middle:], ""
    right_rows, right_timeouts, right_deferred, right_error = _search_shard_with_deadline(
        rg,
        pattern,
        files[middle:],
        cwd=cwd,
        preview_chars=preview_chars,
        ignore_case=ignore_case,
        fixed_string=fixed_string,
        shard_timeout_seconds=shard_timeout_seconds,
        deadline=deadline,
    )
    if right_error:
        return [], [], [], right_error
    return left_rows + right_rows, left_timeouts + right_timeouts, right_deferred, ""


def _normalize_match_record(
    repo_root: Path,
    raw_path: str,
    line_number: int,
    column: int,
    byte_offset: int,
    raw_match: bytes,
) -> tuple[str, int, int, int, int | None, str, int]:
    raw_candidate = Path(raw_path)
    try:
        absolute_candidate = raw_candidate if raw_candidate.is_absolute() else repo_root / raw_candidate
        rel_path = absolute_candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel_path = str(raw_path).replace("\\", "/")
        if rel_path.startswith("./"):
            rel_path = rel_path[2:]
    omitted = raw_match == b"[Omitted long matching line]"
    sensitive_path = indexing_policy.is_explicit_sensitive_path(repo_root / rel_path)
    match_preview = "" if omitted else raw_match.decode("utf-8", errors="replace")
    if sensitive_path and not omitted:
        match_preview = "<REDACTED_SENSITIVE_FILE_MATCH>"
    else:
        match_preview, _ = safety.redact_source_text(match_preview)
    match_bytes = None if omitted else len(raw_match)
    return rel_path, line_number, column, byte_offset, match_bytes, match_preview, int(omitted)


def _resume_materialized_scan(
    cache_path: Path,
    *,
    search_id: str,
    repo_root: Path,
    pattern: str,
    preview_chars: int,
    ignore_case: bool,
    fixed_string: bool,
    shard_timeout_seconds: float,
    operation_timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    budget = max(0.0, min(float(operation_timeout_seconds), MAX_OPERATION_TIMEOUT_SECONDS))
    deadline = None if budget <= 0 else started + budget

    with _cache_lock(cache_path):
        run = _load_run(cache_path, search_id)
        if not run:
            return {"status": "expired_cursor", "reason": "materialized search state is unavailable; restart without a cursor"}
        if run.get("error_status"):
            return {"status": str(run["error_status"]), "reason": str(run.get("error_reason") or "cached search failure")}

        # A completed materialized search can be paged without invoking the scanner.
        # This keeps transport continuation independent from host/runtime rg availability.
        with closing(_cache_connect(cache_path)) as conn:
            pending_before = int(conn.execute(
                "SELECT COUNT(*) FROM search_files WHERE search_id=? AND status='pending'", (search_id,)
            ).fetchone()[0])
            timeout_before = int(conn.execute(
                "SELECT COUNT(*) FROM search_files WHERE search_id=? AND status='timed_out'", (search_id,)
            ).fetchone()[0])
        if pending_before == 0:
            return {
                "status": "ok" if timeout_before == 0 else "partial",
                "scan_complete": True,
                "universe_complete": timeout_before == 0,
                "resume_required": False,
                "pending_file_count": 0,
            }

        rg = shutil.which("rg")
        if not rg:
            return {
                "status": "error",
                "reason": "ripgrep (rg) is required to continue exhaustive discovery",
                "scanner_available": False,
                "scan_complete": False,
                "universe_complete": False,
                "resume_required": True,
                "pending_file_count": pending_before,
            }

        while True:
            if deadline is not None and time.monotonic() >= deadline - OPERATION_DEADLINE_RESERVE_SECONDS:
                break
            with closing(_cache_connect(cache_path)) as conn:
                pending_rows = conn.execute(
                    "SELECT path FROM search_files WHERE search_id=? AND status='pending' ORDER BY ordinal LIMIT ?",
                    (search_id, MAX_FILES_PER_SHARD),
                ).fetchall()
            pending = [str(row["path"]) for row in pending_rows]
            if not pending:
                break

            chunk_started = time.monotonic()
            rows, terminal_timeouts, deferred, error = _search_shard_with_deadline(
                rg,
                pattern,
                pending,
                cwd=repo_root,
                preview_chars=preview_chars,
                ignore_case=ignore_case,
                fixed_string=fixed_string,
                shard_timeout_seconds=shard_timeout_seconds,
                deadline=deadline,
            )
            if error:
                status = "invalid_search" if "regex parse error" in error.lower() else "error"
                with closing(_cache_connect(cache_path)) as conn, conn:
                    conn.execute(
                        "UPDATE search_runs SET error_status=?, error_reason=?, updated_at=? WHERE search_id=?",
                        (status, error, time.time(), search_id),
                    )
                return {"status": status, "reason": error}

            deferred_set = set(deferred)
            timeout_set = set(terminal_timeouts)
            completed_files = [path for path in pending if path not in deferred_set and path not in timeout_set]
            normalized = [
                _normalize_match_record(repo_root, *row)
                for row in rows
            ]
            elapsed_ms = int((time.monotonic() - chunk_started) * 1000)
            with closing(_cache_connect(cache_path)) as conn, conn:
                current_count = int(conn.execute(
                    "SELECT COUNT(*) FROM search_matches WHERE search_id=?", (search_id,)
                ).fetchone()[0])
                conn.executemany(
                    """
                    INSERT INTO search_matches(
                        search_id, match_index, path, line, column_number, byte_offset,
                        match_bytes, match_preview, preview_truncated
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            search_id,
                            current_count + index,
                            rel_path,
                            line_number,
                            column,
                            byte_offset,
                            match_bytes,
                            match_preview,
                            preview_truncated,
                        )
                        for index, (
                            rel_path,
                            line_number,
                            column,
                            byte_offset,
                            match_bytes,
                            match_preview,
                            preview_truncated,
                        ) in enumerate(normalized)
                    ],
                )
                if completed_files:
                    conn.executemany(
                        "UPDATE search_files SET status='done' WHERE search_id=? AND path=?",
                        [(search_id, path) for path in completed_files],
                    )
                if terminal_timeouts:
                    conn.executemany(
                        "UPDATE search_files SET status='timed_out' WHERE search_id=? AND path=?",
                        [(search_id, path) for path in terminal_timeouts],
                    )
                    prior = json.loads(str(run.get("timed_out_files_json") or "[]"))
                    merged = sorted(set(str(value) for value in prior) | set(terminal_timeouts))
                    run["timed_out_files_json"] = json.dumps(merged)
                conn.execute(
                    """
                    UPDATE search_runs
                    SET timed_out_files_json=?, scan_duration_ms=scan_duration_ms+?, updated_at=?
                    WHERE search_id=?
                    """,
                    (str(run.get("timed_out_files_json") or "[]"), elapsed_ms, time.time(), search_id),
                )

            if deferred:
                break

        with closing(_cache_connect(cache_path)) as conn:
            pending_count = int(conn.execute(
                "SELECT COUNT(*) FROM search_files WHERE search_id=? AND status='pending'", (search_id,)
            ).fetchone()[0])
            timeout_count = int(conn.execute(
                "SELECT COUNT(*) FROM search_files WHERE search_id=? AND status='timed_out'", (search_id,)
            ).fetchone()[0])
        return {
            "status": "ok" if pending_count == 0 and timeout_count == 0 else "partial",
            "scan_complete": pending_count == 0,
            "universe_complete": pending_count == 0 and timeout_count == 0,
            "resume_required": pending_count > 0,
            "pending_file_count": pending_count,
        }


def _line_metadata(repo_root: Path, rel_path: str, byte_offset: int, column: int, preview_chars: int) -> tuple[int | None, str, bool, bool]:
    """Return line byte length and bounded context without serializing a giant line."""
    path = Path(rel_path)
    if not path.is_absolute():
        path = repo_root / path
    try:
        line_start = max(0, int(byte_offset) - max(0, int(column) - 1))
        context_bytes = max(96, min(preview_chars, MAX_PREVIEW_CHARS))
        with path.open("rb") as handle:
            handle.seek(line_start)
            line = handle.readline()
        raw_line = line[:-1] if line.endswith(b"\n") else line
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        line_bytes = len(raw_line)
        if len(raw_line) <= context_bytes:
            preview_raw = raw_line
            prefix = suffix = False
        else:
            local = max(0, int(byte_offset) - line_start)
            preview_start = max(0, local - context_bytes // 2)
            preview_end = min(len(raw_line), preview_start + context_bytes)
            if preview_end - preview_start < context_bytes:
                preview_start = max(0, preview_end - context_bytes)
            preview_raw = raw_line[preview_start:preview_end]
            prefix = preview_start > 0
            suffix = preview_end < len(raw_line)
        if indexing_policy.is_explicit_sensitive_path(path):
            return line_bytes, "<REDACTED_SENSITIVE_FILE_CONTEXT>", bool(prefix or suffix), True
        preview = preview_raw.decode("utf-8", errors="replace")
        preview, redacted = safety.redact_source_text(preview)
        if prefix:
            preview = "…" + preview
        if suffix:
            preview += "…"
        return line_bytes, preview, bool(prefix or suffix), redacted
    except OSError:
        return None, "", False, False


def scan_files(*, repo_root: Path, pattern: str, files: list[str], offset: int = 0, page_size: int = DEFAULT_PAGE_SIZE, preview_chars: int = DEFAULT_PREVIEW_CHARS, ignore_case: bool = False, fixed_string: bool = False, shard_timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Exhaustively scan explicit files while bounding each returned match representation."""
    rg = shutil.which("rg")
    if not rg:
        return {"status": "error", "reason": "ripgrep (rg) is required"}
    page_size = _safe_int(page_size, 1, MAX_PAGE_SIZE)
    preview_chars = _safe_int(preview_chars, 64, MAX_PREVIEW_CHARS)
    ordered_files = sorted(dict.fromkeys(str(path).replace("\\", "/") for path in files))
    match_count = 0
    file_counts: Counter[str] = Counter()
    page_rows: list[dict[str, Any]] = []
    timed_out_files: list[str] = []
    started = time.monotonic()

    for shard in _chunk_files(ordered_files):
        rows, shard_timeouts, error = _search_shard_resilient(
            rg, pattern, shard, cwd=repo_root, preview_chars=preview_chars,
            ignore_case=ignore_case, fixed_string=fixed_string,
            timeout_seconds=max(0.0, float(shard_timeout_seconds)),
        )
        if error:
            return {
                "status": "invalid_search" if "regex parse error" in error.lower() else "error",
                "reason": error,
                "match_count": match_count,
                "matching_file_count": len(file_counts),
                "universe_complete": False,
            }
        timed_out_files.extend(shard_timeouts)
        for raw_path, line_number, column, byte_offset, raw_match in rows:
            raw_candidate = Path(raw_path)
            try:
                absolute_candidate = raw_candidate if raw_candidate.is_absolute() else repo_root / raw_candidate
                rel_path = absolute_candidate.resolve().relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                rel_path = str(raw_path).replace("\\", "/")
                if rel_path.startswith("./"):
                    rel_path = rel_path[2:]
            file_counts[rel_path] += 1
            current_index = match_count
            match_count += 1
            if current_index < offset or len(page_rows) >= page_size:
                continue
            omitted = raw_match == b"[Omitted long matching line]"
            sensitive_path = indexing_policy.is_explicit_sensitive_path(repo_root / rel_path)
            match_preview = "" if omitted else raw_match.decode("utf-8", errors="replace")
            if sensitive_path and not omitted:
                match_preview, match_redacted = "<REDACTED_SENSITIVE_FILE_MATCH>", True
            else:
                match_preview, match_redacted = safety.redact_source_text(match_preview)
            match_bytes = None if omitted else len(raw_match)
            line_bytes, context_preview, context_truncated, context_redacted = _line_metadata(
                repo_root, rel_path, byte_offset, column, preview_chars
            )
            page_rows.append({
                "index": current_index,
                "path": rel_path,
                "line": line_number,
                "column": column,
                "byte_offset": byte_offset,
                "match_bytes": match_bytes,
                "match_preview": match_preview,
                "preview_truncated": bool(omitted),
                "match_redacted": match_redacted,
                "line_bytes": line_bytes,
                "context_preview": context_preview,
                "context_truncated": context_truncated,
                "context_redacted": context_redacted,
            })

    universe_complete = not timed_out_files
    duration_ms = int((time.monotonic() - started) * 1000)
    page_end = offset + len(page_rows)
    search_complete = universe_complete and page_end >= match_count
    repository_search_complete = search_complete
    inventory = [
        {"path": path, "matches": count}
        for path, count in sorted(file_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "status": "ok" if universe_complete else "partial",
        "engine": "ripgrep",
        "eligible_universe_complete": universe_complete,
        "repository_universe_complete": universe_complete,
        "universe_complete": universe_complete,
        "match_count": match_count,
        "matching_file_count": len(file_counts),
        "matches": page_rows,
        "offset": offset,
        "page_size": page_size,
        "returned": len(page_rows),
        "search_complete": search_complete,
        "repository_search_complete": repository_search_complete,
        "timed_out_files": timed_out_files,
        "duration_ms": duration_ms,
        "file_inventory": inventory[:MAX_INVENTORY_FILES],
        "file_inventory_complete": len(inventory) <= MAX_INVENTORY_FILES,
        "file_inventory_total": len(inventory),
        "file_inventory_hash": _sha("\n".join(f"{item['path']}\0{item['matches']}" for item in inventory)),
    }


def _render_materialized_result(
    cache_path: Path,
    *,
    search_id: str,
    repo_root: Path,
    offset: int,
    page_size: int,
    preview_chars: int,
) -> dict[str, Any]:
    with closing(_cache_connect(cache_path)) as conn:
        run_row = conn.execute("SELECT * FROM search_runs WHERE search_id=?", (search_id,)).fetchone()
        if not run_row:
            return {"status": "expired_cursor", "reason": "materialized search state is unavailable; restart without a cursor"}
        run = dict(run_row)
        match_count = int(conn.execute(
            "SELECT COUNT(*) FROM search_matches WHERE search_id=?", (search_id,)
        ).fetchone()[0])
        pending_count = int(conn.execute(
            "SELECT COUNT(*) FROM search_files WHERE search_id=? AND status='pending'", (search_id,)
        ).fetchone()[0])
        timed_out_rows = conn.execute(
            "SELECT path FROM search_files WHERE search_id=? AND status='timed_out' ORDER BY ordinal",
            (search_id,),
        ).fetchall()
        timed_out_files = [str(row["path"]) for row in timed_out_rows]
        match_rows = conn.execute(
            """
            SELECT * FROM search_matches
            WHERE search_id=? AND match_index>=?
            ORDER BY match_index LIMIT ?
            """,
            (search_id, offset, page_size),
        ).fetchall()
        inventory_rows = conn.execute(
            """
            SELECT path, COUNT(*) AS matches
            FROM search_matches WHERE search_id=?
            GROUP BY path ORDER BY matches DESC, path ASC
            """,
            (search_id,),
        ).fetchall()

    page_rows: list[dict[str, Any]] = []
    for row in match_rows:
        item = dict(row)
        line_bytes, context_preview, context_truncated, context_redacted = _line_metadata(
            repo_root,
            str(item["path"]),
            int(item["byte_offset"]),
            int(item["column_number"]),
            preview_chars,
        )
        sensitive_path = indexing_policy.is_explicit_sensitive_path(repo_root / str(item["path"]))
        if sensitive_path and str(item["match_preview"]):
            match_preview, match_redacted = "<REDACTED_SENSITIVE_FILE_MATCH>", True
        else:
            match_preview, match_redacted = safety.redact_source_text(str(item["match_preview"]))
        page_rows.append({
            "index": int(item["match_index"]),
            "path": str(item["path"]),
            "source_role": indexing_policy.source_role(str(item["path"])),
            "line": int(item["line"]),
            "column": int(item["column_number"]),
            "byte_offset": int(item["byte_offset"]),
            "match_bytes": item["match_bytes"],
            "match_preview": match_preview,
            "preview_truncated": bool(item["preview_truncated"]),
            "match_redacted": match_redacted,
            "line_bytes": line_bytes,
            "context_preview": context_preview,
            "context_truncated": context_truncated,
            "context_redacted": context_redacted,
        })

    scan_complete = pending_count == 0
    eligible_universe_complete = scan_complete and not timed_out_files and not run.get("error_status")
    excluded_source_count = int(run.get("policy_excluded_source_file_count") or 0)
    repository_universe_complete = eligible_universe_complete and excluded_source_count == 0
    returned = len(page_rows)
    page_end = offset + returned
    search_complete = eligible_universe_complete and page_end >= match_count
    repository_search_complete = search_complete and repository_universe_complete
    needs_more_transport = page_end < match_count
    needs_more_scan = pending_count > 0
    next_cursor = ""
    if needs_more_transport or needs_more_scan:
        next_cursor = _encode_cursor(page_end, str(run["fingerprint"]), search_id)
    inventory = [
        {"path": str(row["path"]), "source_role": indexing_policy.source_role(str(row["path"])), "matches": int(row["matches"])}
        for row in inventory_rows
    ]
    return {
        "status": "ok" if repository_universe_complete else "partial",
        "engine": "ripgrep",
        "search_id": search_id,
        "scan_materialized": True,
        "scan_complete": scan_complete,
        "eligible_universe_complete": eligible_universe_complete,
        "repository_universe_complete": repository_universe_complete,
        "universe_complete": repository_universe_complete,
        "resume_required": needs_more_scan,
        "pending_file_count": pending_count,
        "match_count": match_count,
        "match_count_final": scan_complete,
        "matching_file_count": len(inventory),
        "matches": page_rows,
        "offset": offset,
        "page_size": page_size,
        "returned": returned,
        "search_complete": search_complete,
        "repository_search_complete": repository_search_complete,
        "timed_out_files": timed_out_files,
        "duration_ms": int(run.get("scan_duration_ms") or 0),
        "file_inventory": inventory[:MAX_INVENTORY_FILES],
        "file_inventory_complete": scan_complete and len(inventory) <= MAX_INVENTORY_FILES,
        "file_inventory_total": len(inventory),
        "file_inventory_final": scan_complete,
        "file_inventory_hash": _sha("\n".join(f"{item['path']}\0{item['matches']}" for item in inventory)),
        "next_cursor": next_cursor,
        "operation_budget_exhausted": needs_more_scan,
        "eligibility_source": str(run.get("eligibility_source") or ""),
    }


def _cursor_request_matches_run(
    run: dict[str, Any],
    *,
    project_id: str,
    pattern: str,
    scopes: list[str],
    ignore_case: bool,
    fixed_string: bool,
    include_ignored: bool,
    preview_chars: int,
    cursor_fingerprint: str,
) -> bool:
    try:
        run_scopes = json.loads(str(run.get("scopes_json") or "[]"))
    except json.JSONDecodeError:
        return False
    return bool(
        run.get("project_id") == project_id
        and run.get("fingerprint") == cursor_fingerprint
        and run.get("pattern") == pattern
        and run_scopes == scopes
        and bool(run.get("ignore_case")) == bool(ignore_case)
        and bool(run.get("fixed_string")) == bool(fixed_string)
        and bool(run.get("include_ignored")) == bool(include_ignored)
        and int(run.get("preview_chars") or 0) == int(preview_chars)
    )


def search_project_text(
    paths: Any,
    project_id: str,
    pattern: str,
    *,
    search_paths: list[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: str = "",
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ignore_case: bool = False,
    fixed_string: bool = False,
    include_ignored: bool = False,
    shard_timeout_seconds: float = 15.0,
    operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    repo: str = "",
) -> dict[str, Any]:
    """Policy-gated exhaustive search with one-time materialization and resumable pages."""
    if not pattern or len(pattern.encode("utf-8", errors="replace")) > MAX_PATTERN_BYTES:
        return {"status": "rejected", "reason": f"pattern must be non-empty and at most {MAX_PATTERN_BYTES} UTF-8 bytes"}
    scopes, scope_error = _normalize_scope_paths(search_paths)
    if scope_error:
        return {"status": "rejected", "reason": scope_error}
    page_size = _safe_int(page_size, 1, MAX_PAGE_SIZE)
    preview_chars = _safe_int(preview_chars, 64, MAX_PREVIEW_CHARS)
    try:
        offset, cursor_fingerprint, cursor_search_id = _decode_cursor(cursor)
    except ValueError as exc:
        return {"status": "rejected", "project_id": project_id, "reason": str(exc)}

    pp = project_workspace.paths_for(paths.root, project_id)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": project_id}
    resolved = project_workspace.resolve_project_repository(paths.root, project_id, repo, require_unique=True)
    if resolved.get("status") != "ok":
        return {k: v for k, v in resolved.items() if k != "root"}
    repo_root = Path(resolved["root"])
    if not repo_root.exists():
        return {"status": "not_found", "project_id": project_id, "repo_id": resolved.get("repo_id"), "reason": "repository directory does not exist"}

    branch = engine.branch_identity(project_id, repo_root, repo_name=str(resolved.get("repo_id") or "default"), legacy=bool(resolved.get("legacy")))
    if branch.source in {"nested_git_root_mismatch", "git_root_mismatch"}:
        return {
            "status": "invalid_repo_root",
            "project_id": project_id,
            "repo_root": str(repo_root),
            "branch": branch.__dict__,
            "reason": "configured project repo/ is not the exact Git worktree root",
            "nested_git_roots": engine._nested_git_roots(repo_root),
        }
    cache_path = _cache_path(pp.project_dir)
    _init_cache(cache_path)
    _prune_cache(cache_path)

    if cursor_search_id:
        run = _load_run(cache_path, cursor_search_id)
        if not run:
            return {
                "status": "expired_cursor",
                "project_id": project_id,
                "reason": "materialized search state expired or was pruned; restart without a cursor",
                "restart_cursor": "",
            }
        if not _cursor_request_matches_run(
            run,
            project_id=project_id,
            pattern=pattern,
            scopes=scopes,
            ignore_case=ignore_case,
            fixed_string=fixed_string,
            include_ignored=include_ignored,
            preview_chars=preview_chars,
            cursor_fingerprint=cursor_fingerprint,
        ):
            return {
                "status": "stale_cursor",
                "project_id": project_id,
                "reason": "search pattern, scope, options, or cached search identity changed; restart without a cursor",
                "restart_cursor": "",
                "branch": branch.__dict__,
            }
        try:
            cached_branch = json.loads(str(run.get("branch_json") or "{}"))
        except json.JSONDecodeError:
            cached_branch = {}
        if (
            cached_branch.get("branch_key") != branch.branch_key
            or str(cached_branch.get("commit_sha") or "") != str(branch.commit_sha or "")
            or bool(cached_branch.get("dirty")) != bool(branch.dirty)
        ):
            return {
                "status": "stale_cursor",
                "project_id": project_id,
                "reason": "repository branch, commit, or dirty state changed; restart without a cursor",
                "restart_cursor": "",
                "branch": branch.__dict__,
                "source_probe_hash": str(run.get("source_probe_hash") or ""),
            }

        current_token = _quick_source_token(
            repo_root, branch, include_ignored=include_ignored,
            trusted_snapshot=str(run.get("eligibility_source") or "") == "index_manifest",
        )
        if current_token:
            source_current = current_token == str(run.get("source_token") or "")
        else:
            included, excluded, _, _ = _eligibility_snapshot(
                paths, project_id, repo_root, branch, include_ignored=include_ignored
            )
            scoped_rows = [
                row for row in included + excluded
                if _in_scope(str(row.get("repo_relative") or ""), scopes)
            ]
            source_current = _scope_probe_hash(scoped_rows) == str(run.get("source_probe_hash") or "")
        if not source_current:
            return {
                "status": "stale_cursor",
                "project_id": project_id,
                "reason": "repository source changed since the search was materialized; restart without a cursor",
                "restart_cursor": "",
                "branch": branch.__dict__,
                "source_probe_hash": str(run.get("source_probe_hash") or ""),
            }

        resumed = _resume_materialized_scan(
            cache_path,
            search_id=cursor_search_id,
            repo_root=repo_root,
            pattern=pattern,
            preview_chars=preview_chars,
            ignore_case=ignore_case,
            fixed_string=fixed_string,
            shard_timeout_seconds=shard_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if resumed.get("status") in {"invalid_search", "expired_cursor"}:
            return {**resumed, "project_id": project_id, "branch": branch.__dict__}
        scan = _render_materialized_result(
            cache_path,
            search_id=cursor_search_id,
            repo_root=repo_root,
            offset=offset,
            page_size=page_size,
            preview_chars=preview_chars,
        )
        if resumed.get("status") == "error":
            scan.update({
                "status": "error",
                "reason": str(resumed.get("reason") or "text scanner unavailable"),
                "scanner_available": bool(resumed.get("scanner_available", False)),
            })
        run = _load_run(cache_path, cursor_search_id) or run
        result_fingerprint = str(run.get("fingerprint") or cursor_fingerprint)
        source_probe_hash = str(run.get("source_probe_hash") or "")
        cache_hit = True
    else:
        included, excluded, _, eligibility_source = _eligibility_snapshot(
            paths, project_id, repo_root, branch, include_ignored=include_ignored
        )
        scoped_included = [row for row in included if _in_scope(str(row.get("repo_relative") or ""), scopes)]
        scoped_excluded = [row for row in excluded if _in_scope(str(row.get("repo_relative") or ""), scopes)]
        source_probe_hash = _scope_probe_hash(scoped_included + scoped_excluded)
        source_token = _quick_source_token(
            repo_root, branch, include_ignored=include_ignored,
            trusted_snapshot=eligibility_source == "index_manifest",
        ) or _sha(f"full-policy\0{source_probe_hash}")
        result_fingerprint = _fingerprint(
            pattern=pattern,
            scopes=scopes,
            ignore_case=ignore_case,
            fixed_string=fixed_string,
            preview_chars=preview_chars,
            source_probe_hash=source_probe_hash,
            source_token=source_token,
            branch_key=branch.branch_key,
            include_ignored=include_ignored,
        )
        search_id = result_fingerprint
        cache_hit = _load_run(cache_path, search_id) is not None
        excluded_reasons = Counter(str(row.get("reason") or "unknown") for row in scoped_excluded)
        excluded_source_count, excluded_source_reasons = _source_exclusion_summary(repo_root, scoped_excluded)
        _initialize_run(
            cache_path,
            search_id=search_id,
            project_id=project_id,
            fingerprint=result_fingerprint,
            source_token=source_token,
            source_probe_hash=source_probe_hash,
            branch=branch,
            pattern=pattern,
            scopes=scopes,
            ignore_case=ignore_case,
            fixed_string=fixed_string,
            include_ignored=include_ignored,
            preview_chars=preview_chars,
            files=sorted(str(row["repo_relative"]) for row in scoped_included),
            excluded_reasons=excluded_reasons,
            excluded_count=len(scoped_excluded),
            excluded_source_reasons=excluded_source_reasons,
            excluded_source_count=excluded_source_count,
            eligibility_source=eligibility_source,
        )
        resumed = _resume_materialized_scan(
            cache_path,
            search_id=search_id,
            repo_root=repo_root,
            pattern=pattern,
            preview_chars=preview_chars,
            ignore_case=ignore_case,
            fixed_string=fixed_string,
            shard_timeout_seconds=shard_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if resumed.get("status") in {"invalid_search", "expired_cursor"}:
            return {
                **resumed,
                "project_id": project_id,
                "branch": branch.__dict__,
                "source_probe_hash": source_probe_hash,
            }
        scan = _render_materialized_result(
            cache_path,
            search_id=search_id,
            repo_root=repo_root,
            offset=0,
            page_size=page_size,
            preview_chars=preview_chars,
        )
        if resumed.get("status") == "error":
            scan.update({
                "status": "error",
                "reason": str(resumed.get("reason") or "text scanner unavailable"),
                "scanner_available": bool(resumed.get("scanner_available", False)),
            })

    run = _load_run(cache_path, str(scan.get("search_id") or cursor_search_id)) or {}
    try:
        policy_excluded_reasons = json.loads(str(run.get("policy_excluded_reasons_json") or "{}"))
    except json.JSONDecodeError:
        policy_excluded_reasons = {}
    try:
        policy_excluded_source_reasons = json.loads(str(run.get("policy_excluded_source_reasons_json") or "{}"))
    except json.JSONDecodeError:
        policy_excluded_source_reasons = {}
    scope_constraints = provenance.repository_scope_constraints(repo_root)
    missing_tracked = int(scope_constraints.get("unmaterialized_tracked_file_count") or 0)
    repository_universe_complete = bool(scan.get("repository_universe_complete")) and missing_tracked == 0
    repository_search_complete = bool(scan.get("search_complete")) and repository_universe_complete
    scan_status = str(scan.get("status") or "partial")
    effective_status = scan_status if scan_status == "error" else (scan_status if repository_universe_complete else "partial")
    return {
        **scan,
        "status": effective_status,
        "repository_universe_complete": repository_universe_complete,
        "universe_complete": repository_universe_complete,
        "repository_search_complete": repository_search_complete,
        "project_id": project_id,
        "mode": "exhaustive_text_fallback",
        "pattern": pattern,
        "paths": scopes or ["."],
        "branch": branch.__dict__,
        "repository_scope": (
            ("git_tracked_visible_untracked_and_ignored" if include_ignored else "git_tracked_and_visible_untracked")
            if str(getattr(branch, "source", "")).startswith("git_")
            else "filesystem_textual_candidates"
        ),
        "include_ignored": bool(include_ignored),
        "git_ignored_paths_scanned": bool(include_ignored) if str(getattr(branch, "source", "")).startswith("git_") else None,
        "repository_scope_constraints": scope_constraints,
        "source_probe_hash": source_probe_hash,
        "result_fingerprint": result_fingerprint,
        "eligible_file_count": int(run.get("eligible_file_count") or 0),
        "repository_source_file_count": int(run.get("repository_source_file_count") or 0),
        "policy_excluded_file_count": int(run.get("policy_excluded_file_count") or 0),
        "policy_excluded_reasons": policy_excluded_reasons,
        "policy_excluded_source_file_count": int(run.get("policy_excluded_source_file_count") or 0),
        "policy_excluded_source_reasons": policy_excluded_source_reasons,
        "cache_hit": cache_hit,
        "evidence_level": "DISCOVERY ONLY",
        "notes": [
            "eligible_universe_complete describes scanner coverage over policy-eligible source; repository_universe_complete is true only when no source file in the declared repository_scope was policy-excluded. Git worktrees default to tracked plus visible untracked files and honor .gitignore; include_ignored=true explicitly expands forensic lexical scope to ignored untracked files. Sparse worktrees with unmaterialized tracked paths are reported as repository-incomplete instead of silently claiming coverage. Submodule worktrees are separate repositories and are reported as an explicit scope boundary. The legacy universe_complete field aliases repository_universe_complete.",
            "The exhaustive match universe is materialized once per source snapshot/query and later pages reuse it instead of rescanning completed files.",
            "If scan_complete=false, next_cursor resumes unfinished discovery within the operation deadline; match_count is final only when match_count_final=true.",
            "Pagination bounds only the returned match representation; it does not cap final total matches or candidate files.",
            "Giant lines are represented by byte/line locations and bounded previews rather than serialized in full.",
            "Reopen relevant current source with code_source_window before asserting behavior.",
        ],
    }
