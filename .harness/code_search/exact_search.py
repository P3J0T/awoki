from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

import indexing_policy
import runtime_safety
import safety

MAX_PATTERNS = 8
MAX_PATTERN_BYTES = 4096
MAX_GLOBS = 32
MAX_PATHS = 32
MAX_RESULTS = 2000
MAX_CONTEXT = 20
MAX_LINE_CHARS = 1200
MAX_TIMEOUT_SECONDS = 60.0


def _clean_rel_paths(values: Iterable[str], *, max_items: int) -> tuple[list[str], str]:
    raw_values = list(values)
    if len(raw_values) > max_items:
        return [], f"at most {max_items} paths are supported per call"
    out: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        p = Path(text)
        if p.is_absolute() or ".." in p.parts:
            return [], f"path must be repository-relative and may not escape the repository: {text}"
        normalized = p.as_posix()
        if normalized == ".":
            normalized = ""
        if normalized and normalized not in out:
            out.append(normalized)
    return out, ""


def _clean_globs(values: Iterable[str], *, max_items: int) -> tuple[list[str], str]:
    raw_values = list(values)
    if len(raw_values) > max_items:
        return [], f"at most {max_items} globs are supported per call"
    out: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        if "\x00" in text or "\n" in text or "\r" in text:
            return [], "glob contains unsupported control characters"
        if text.startswith("!"):
            text = text[1:]
        if text and text not in out:
            out.append(text)
    return out, ""


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _patterns(values: Iterable[str], fixed_strings: bool) -> tuple[list[str], str]:
    raw_values = list(values)
    if len(raw_values) > MAX_PATTERNS:
        return [], f"at most {MAX_PATTERNS} patterns are supported per call"
    out: list[str] = []
    for raw in raw_values:
        text = str(raw or "")
        if not text:
            continue
        if len(text.encode("utf-8", errors="replace")) > MAX_PATTERN_BYTES:
            return [], f"each pattern must be at most {MAX_PATTERN_BYTES} UTF-8 bytes"
        if not fixed_strings:
            try:
                re.compile(text)
            except re.error:
                # Python regex and ripgrep's Rust regex differ. Do not reject here;
                # rg remains authoritative for syntax and reports structured failure.
                pass
        out.append(text)
    if not out:
        return [], "at least one non-empty pattern is required"
    return out, ""


def _rel(repo_root: Path, raw: str) -> str:
    candidate = Path(raw)
    try:
        absolute = candidate if candidate.is_absolute() else repo_root / candidate
        return absolute.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        text = raw.replace("\\", "/")
        return text[2:] if text.startswith("./") else text


def _redact_line(repo_root: Path, rel_path: str, text: str) -> tuple[str, bool, bool]:
    clean = text.rstrip("\r\n")
    truncated = len(clean) > MAX_LINE_CHARS
    if truncated:
        clean = clean[:MAX_LINE_CHARS]
    if indexing_policy.is_explicit_sensitive_path(repo_root / rel_path):
        return "<REDACTED_SENSITIVE_FILE_LINE>", truncated, True
    redacted, changed = safety.redact_source_text(clean)
    return redacted, truncated, bool(changed)


def _base_args(
    rg: str,
    *,
    patterns: list[str],
    paths: list[str],
    include_globs: list[str],
    exclude_globs: list[str],
    ignore_case: bool,
    fixed_strings: bool,
    hidden: bool,
    include_ignored: bool,
) -> list[str]:
    args = [rg, "--no-messages", "--color", "never", "--sort", "path"]
    if ignore_case:
        args.append("--ignore-case")
    if fixed_strings:
        args.append("--fixed-strings")
    if hidden:
        args.append("--hidden")
    if include_ignored:
        args.append("--no-ignore")
    for glob in include_globs:
        args.extend(["--glob", glob])
    for glob in exclude_globs:
        args.extend(["--glob", f"!{glob}"])
    for pattern in patterns:
        args.extend(["-e", pattern])
    args.append("--")
    args.extend(paths or ["."])
    return args


def _spawn(args: list[str], *, cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        args,
        cwd=cwd,
        env=runtime_safety.credential_free_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait(proc: subprocess.Popen[bytes], *, timeout: float, started: float) -> tuple[int, str, bool]:
    remaining = max(0.05, timeout - (time.monotonic() - started))
    try:
        _, stderr = proc.communicate(timeout=remaining)
        return int(proc.returncode or 0), stderr.decode("utf-8", errors="replace")[:4000], False
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
        return int(proc.returncode or -9), stderr.decode("utf-8", errors="replace")[:4000], True


def _matches_mode(
    args: list[str],
    *,
    repo_root: Path,
    offset: int,
    limit: int,
    context_before: int,
    context_after: int,
    timeout: float,
) -> dict[str, Any]:
    command = [*args[:1], "--json", *args[1:]]
    if context_before:
        command[1:1] = ["--before-context", str(context_before)]
    if context_after:
        command[1:1] = ["--after-context", str(context_after)]
    proc = _spawn(command, cwd=repo_root)
    assert proc.stdout is not None
    started = time.monotonic()
    page: list[dict[str, Any]] = []
    pending_context: list[dict[str, Any]] = []
    selected_matches = 0
    seen_matches = 0
    has_more = False
    timed_out = False
    stop_after_context = False

    while True:
        if time.monotonic() - started > timeout:
            timed_out = True
            proc.kill()
            break
        raw = proc.stdout.readline()
        if not raw:
            break
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type not in {"match", "context"}:
            continue
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        line_data = data.get("lines") if isinstance(data.get("lines"), dict) else {}
        raw_path = str(path_data.get("text") or "")
        rel_path = _rel(repo_root, raw_path)
        line_number = int(data.get("line_number") or 0)
        line_text, line_truncated, redacted = _redact_line(repo_root, rel_path, str(line_data.get("text") or ""))
        row = {
            "type": event_type,
            "path": rel_path,
            "source_role": indexing_policy.source_role(rel_path),
            "line": line_number,
            "line_text": line_text,
            "line_truncated": line_truncated,
            "redacted": redacted,
        }
        if event_type == "context":
            if selected_matches > 0:
                page.append(row)
                if stop_after_context:
                    # Context after the final returned match belongs to that match.
                    continue
            else:
                pending_context.append(row)
                if len(pending_context) > context_before:
                    pending_context = pending_context[-context_before:]
            continue

        # Match event.
        if seen_matches >= offset + limit:
            has_more = True
            stop_after_context = True
            break
        if seen_matches >= offset:
            if context_before and pending_context:
                page.extend(pending_context[-context_before:])
            pending_context = []
            subs = []
            for sub in list(data.get("submatches") or [])[:32]:
                if not isinstance(sub, dict):
                    continue
                match = sub.get("match") if isinstance(sub.get("match"), dict) else {}
                raw_match = str(match.get("text") or "")
                if indexing_policy.is_explicit_sensitive_path(repo_root / rel_path):
                    match_text, match_redacted = "<REDACTED_SENSITIVE_MATCH>", True
                else:
                    match_text, match_redacted = safety.redact_source_text(raw_match[:400])
                subs.append({
                    "start": int(sub.get("start") or 0),
                    "end": int(sub.get("end") or 0),
                    "text": match_text,
                    "redacted": bool(match_redacted),
                })
            row["submatches"] = subs
            row["absolute_offset"] = int(data.get("absolute_offset") or 0)
            row["match_index"] = seen_matches
            page.append(row)
            selected_matches += 1
        seen_matches += 1

    returncode, stderr, wait_timed_out = _wait(proc, timeout=timeout, started=started)
    timed_out = timed_out or wait_timed_out
    if returncode not in {0, 1, -9} and not has_more:
        reason = stderr.strip() or f"ripgrep exited with status {returncode}"
        return {"status": "invalid_search" if "regex parse error" in reason.lower() else "error", "reason": reason}
    return {
        "status": "partial" if timed_out else "ok",
        "rows": page,
        "returned_matches": selected_matches,
        "seen_match_count": seen_matches,
        "has_more": has_more,
        "timed_out": timed_out,
        "stderr": stderr if stderr and returncode not in {0, 1, -9} else "",
    }


def _nul_stream_mode(
    args: list[str],
    *,
    repo_root: Path,
    mode: str,
    offset: int,
    limit: int,
    timeout: float,
) -> dict[str, Any]:
    if mode == "files":
        command = [*args[:1], "--files-with-matches", "--null", *args[1:]]
    else:
        command = [*args[:1], "--count-matches", "--null", *args[1:]]
    proc = _spawn(command, cwd=repo_root)
    assert proc.stdout is not None
    started = time.monotonic()
    page: list[dict[str, Any]] = []
    index = 0
    has_more = False
    timed_out = False

    if mode == "files":
        buffer = bytearray()
        while True:
            if time.monotonic() - started > timeout:
                timed_out = True
                proc.kill()
                break
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            while b"\0" in buffer:
                raw_path, _, remainder = buffer.partition(b"\0")
                buffer = bytearray(remainder)
                if not raw_path:
                    continue
                if index >= offset + limit:
                    has_more = True
                    proc.kill()
                    break
                if index >= offset:
                    rel_path = _rel(repo_root, os.fsdecode(bytes(raw_path)))
                    page.append({"index": index, "path": rel_path, "source_role": indexing_policy.source_role(rel_path)})
                index += 1
            if has_more:
                break
    else:
        while True:
            if time.monotonic() - started > timeout:
                timed_out = True
                proc.kill()
                break
            raw = proc.stdout.readline()
            if not raw:
                break
            path_raw, sep, count_raw = raw.partition(b"\0")
            if not sep:
                continue
            if index >= offset + limit:
                has_more = True
                proc.kill()
                break
            if index >= offset:
                rel_path = _rel(repo_root, os.fsdecode(path_raw))
                try:
                    count = int(count_raw.strip() or b"0")
                except ValueError:
                    count = 0
                page.append({"index": index, "path": rel_path, "source_role": indexing_policy.source_role(rel_path), "matches": count})
            index += 1

    returncode, stderr, wait_timed_out = _wait(proc, timeout=timeout, started=started)
    timed_out = timed_out or wait_timed_out
    if returncode not in {0, 1, -9} and not has_more:
        reason = stderr.strip() or f"ripgrep exited with status {returncode}"
        return {"status": "invalid_search" if "regex parse error" in reason.lower() else "error", "reason": reason}
    result: dict[str, Any] = {
        "status": "partial" if timed_out else "ok",
        "rows": page,
        "returned": len(page),
        "seen_result_count": index,
        "has_more": has_more,
        "timed_out": timed_out,
    }
    if mode == "count":
        result["returned_match_count"] = sum(int(row.get("matches") or 0) for row in page)
    return result


def exact_search(
    repo_root: Path,
    *,
    patterns: list[str],
    mode: str = "matches",
    paths: list[str] | None = None,
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
) -> dict[str, Any]:
    rg = shutil.which("rg")
    if not rg:
        return {"status": "blocked", "reason": "ripgrep (rg) is required"}
    selected_mode = str(mode or "matches").strip().lower()
    if selected_mode not in {"matches", "files", "count"}:
        return {"status": "rejected", "reason": "mode must be one of: matches, files, count"}
    clean_patterns, error = _patterns(patterns, fixed_strings)
    if error:
        return {"status": "rejected", "reason": error}
    clean_paths, error = _clean_rel_paths(paths or [], max_items=MAX_PATHS)
    if error:
        return {"status": "rejected", "reason": error}
    clean_includes, error = _clean_globs(include_globs or [], max_items=MAX_GLOBS)
    if error:
        return {"status": "rejected", "reason": error}
    clean_excludes, error = _clean_globs(exclude_globs or [], max_items=MAX_GLOBS)
    if error:
        return {"status": "rejected", "reason": error}
    bounded_offset = _bounded_int(offset, 0, 0, 10_000_000)
    bounded_limit = _bounded_int(limit, 200, 1, MAX_RESULTS)
    before = _bounded_int(context_before, 0, 0, MAX_CONTEXT)
    after = _bounded_int(context_after, 0, 0, MAX_CONTEXT)
    timeout = _bounded_float(timeout_seconds, 20.0, 0.25, MAX_TIMEOUT_SECONDS)
    if selected_mode != "matches" and (before or after):
        return {"status": "rejected", "reason": "context_before/context_after are supported only in mode=matches"}

    args = _base_args(
        rg,
        patterns=clean_patterns,
        paths=clean_paths,
        include_globs=clean_includes,
        exclude_globs=clean_excludes,
        ignore_case=bool(ignore_case),
        fixed_strings=bool(fixed_strings),
        hidden=bool(hidden),
        include_ignored=bool(include_ignored),
    )
    started = time.monotonic()
    if selected_mode == "matches":
        payload = _matches_mode(
            args,
            repo_root=repo_root,
            offset=bounded_offset,
            limit=bounded_limit,
            context_before=before,
            context_after=after,
            timeout=timeout,
        )
    else:
        payload = _nul_stream_mode(
            args,
            repo_root=repo_root,
            mode=selected_mode,
            offset=bounded_offset,
            limit=bounded_limit,
            timeout=timeout,
        )
    if payload.get("status") in {"error", "invalid_search"}:
        return payload
    has_more = bool(payload.get("has_more"))
    returned = int(payload.get("returned_matches") if selected_mode == "matches" else payload.get("returned") or 0)
    next_offset = bounded_offset + returned if has_more else None
    return {
        "status": payload.get("status", "ok"),
        "engine": "ripgrep",
        "structured": True,
        "shell": False,
        "repository_root": str(repo_root),
        "mode": selected_mode,
        "query": {
            "patterns": clean_patterns,
            "paths": clean_paths,
            "include_globs": clean_includes,
            "exclude_globs": clean_excludes,
            "ignore_case": bool(ignore_case),
            "fixed_strings": bool(fixed_strings),
            "hidden": bool(hidden),
            "include_ignored": bool(include_ignored),
            "context_before": before,
            "context_after": after,
        },
        "offset": bounded_offset,
        "limit": bounded_limit,
        "returned": returned,
        "rows": payload.get("rows") or [],
        "has_more": has_more,
        "truncated": has_more,
        "continuation": {
            "available": has_more,
            "next_offset": next_offset,
            "instruction": f"Repeat the same query with offset={next_offset}." if has_more else "",
            "ordering": "path, then ripgrep match order",
            "repository_change_warning": "Restart from offset=0 if repository contents change between pages." if has_more else "",
        },
        "timed_out": bool(payload.get("timed_out")),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "credential_environment": "stripped",
        "policy": {
            "raw_cli_passthrough": False,
            "repository_scoped": True,
            "sensitive_match_redaction": True,
        },
        **{k: v for k, v in payload.items() if k not in {"status", "rows", "has_more", "timed_out", "returned", "returned_matches"}},
    }
