from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_VALUE_CHARS = 4096
PROBE_TIMEOUT_SECONDS = 8.0
HOST_COMPILE_TIMEOUT_SECONDS = 30.0
PREBUILT_HELPER = Path("/usr/local/bin/awoki-go-semantics")
BUNDLED_PROBE_SOURCE = Path(__file__).with_name("go_semantics_probe.go")

LANGUAGE_STABLE_OPERATIONS = {"failed_error_type_assertion"}
STDLIB_OR_RUNTIME_OPERATIONS = {
    "path_join", "path_clean", "parse_duration", "duration_multiply", "strings_replace", "url_parse",
    "reverse_proxy_rewrite_headers",
}
SUPPORTED_OPERATIONS = [
    "path_join", "path_clean", "parse_duration", "duration_multiply",
    "failed_error_type_assertion", "strings_replace", "url_parse",
    "reverse_proxy_rewrite_headers",
]


def _bounded_string(value: Any, field: str) -> str:
    text = str(value)
    if len(text) > MAX_VALUE_CHARS:
        raise ValueError(f"{field} exceeds {MAX_VALUE_CHARS} characters")
    return text


def _trusted_go_binary() -> str:
    # Host-side source-tree validation may compile the fixed bundled helper.
    # The Docker product instead ships a prebuilt helper compiled by the pinned
    # Go builder stage, so the runtime image does not need a Go compiler.
    pinned = Path("/usr/local/go/bin/go")
    if pinned.is_file() and os.access(pinned, os.X_OK):
        return str(pinned)
    return shutil.which("go") or ""


def _trusted_go_cache() -> Path | None:
    """Private host-development cache for compiling only the fixed probe."""
    try:
        root = Path(tempfile.gettempdir()) / f"awoki-go-semantics-cache-{os.getuid()}"
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            return None
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        stat = root.stat()
        if stat.st_uid != os.getuid():
            return None
        if stat.st_mode & 0o077:
            root.chmod(0o700)
        return root
    except OSError:
        return None


def _runtime_env(home: Path, tmpdir: Path) -> dict[str, str]:
    # The prebuilt helper needs no external programs. Keep environment-driven
    # Go/runtime behavior (notably GODEBUG) from silently changing the probe.
    return {
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GODEBUG": "",
        "GOTRACEBACK": "none",
    }


def _go_compile_env(home: Path, tmpdir: Path, cache: Path, modcache: Path) -> dict[str, str]:
    env = _runtime_env(home, tmpdir)
    env.update({
        "GO111MODULE": "off",
        "GOWORK": "off",
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOVCS": "*:off",
        "CGO_ENABLED": "0",
        "GOFLAGS": "",
        "GOCACHE": str(cache),
        "GOMODCACHE": str(modcache),
        "GOTELEMETRY": "off",
    })
    return env


def _execute_probe(operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps(
        {"operation": operation, "inputs": inputs},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with tempfile.TemporaryDirectory(prefix="awoki-go-semantics-") as td:
        root = Path(td)
        home = root / "home"
        tmpdir = root / "tmp"
        home.mkdir()
        tmpdir.mkdir()

        if PREBUILT_HELPER.is_file() and os.access(PREBUILT_HELPER, os.X_OK):
            command = [str(PREBUILT_HELPER)]
            env = _runtime_env(home, tmpdir)
            backend = "prebuilt_pinned_helper"
        else:
            go = _trusted_go_binary()
            if not go:
                return {
                    "status": "unavailable",
                    "language": "go",
                    "reason": "Awoki Go semantics helper is absent and no local Go toolchain is available for the fixed-source fallback",
                    "executed": False,
                }
            if not BUNDLED_PROBE_SOURCE.is_file():
                return {
                    "status": "unavailable",
                    "language": "go",
                    "reason": "bundled Go semantics probe source is missing",
                    "executed": False,
                }
            cache = _trusted_go_cache() or (root / "gocache")
            modcache = root / "gomodcache"
            if cache == root / "gocache":
                cache.mkdir()
            modcache.mkdir()
            command = [go, "run", str(BUNDLED_PROBE_SOURCE)]
            env = _go_compile_env(home, tmpdir, cache, modcache)
            backend = "fixed_source_go_run_fallback"

        timeout_seconds = (
            HOST_COMPILE_TIMEOUT_SECONDS if backend == "fixed_source_go_run_fallback" else PROBE_TIMEOUT_SECONDS
        )
        try:
            completed = subprocess.run(
                command,
                input=request,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
                cwd=td,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "language": "go",
                "reason": f"Go semantics probe exceeded {timeout_seconds:.0f}s",
                "executed": True,
                "execution_backend": backend,
            }
        except OSError as exc:
            return {
                "status": "unavailable",
                "language": "go",
                "reason": str(exc),
                "executed": False,
                "execution_backend": backend,
            }
        if completed.returncode != 0:
            return {
                "status": "error",
                "language": "go",
                "reason": "fixed Go semantics helper failed",
                "stderr": completed.stderr[-4000:],
                "executed": True,
                "returncode": completed.returncode,
                "execution_backend": backend,
            }
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "status": "error",
                "language": "go",
                "reason": "Go semantics helper returned non-JSON output",
                "stdout": completed.stdout[-4000:],
                "executed": True,
                "execution_backend": backend,
            }

    helper_status = str(envelope.get("status") or "") if isinstance(envelope, dict) else ""
    version = str(envelope.get("go_version") or "") if isinstance(envelope, dict) else ""
    version_match = re.search(r"\bgo(\d+\.\d+(?:\.\d+)?)\b", version)
    if helper_status != "ok":
        return {
            "status": "rejected" if helper_status == "rejected" else "error",
            "language": "go",
            "reason": str(envelope.get("reason") or "Go semantics helper rejected the request") if isinstance(envelope, dict) else "invalid helper response",
            "executed": True,
            "execution_backend": backend,
            "toolchain": version,
            "toolchain_version": version_match.group(1) if version_match else "",
        }
    return {
        "status": "ok",
        "language": "go",
        "executed": True,
        "execution_backend": backend,
        "toolchain": version,
        "toolchain_version": version_match.group(1) if version_match else "",
        "network": False,
        "repository_code_executed": False,
        "inherited_go_configuration": False,
        "observed": envelope.get("observed") if isinstance(envelope, dict) else None,
    }


def read_project_go_metadata(repo_root: Path) -> dict[str, Any]:
    """Read Go version directives as plain text; never execute project code."""
    path = Path(repo_root) / "go.mod"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = []
            total = 0
            for raw in handle:
                total += len(raw)
                if total > 256_000 or len(lines) >= 5000:
                    break
                lines.append(raw)
    except OSError:
        return {"go_mod_present": False, "go_version": "", "toolchain": ""}
    go_version = ""
    toolchain = ""
    for raw in lines:
        line = raw.strip()
        if line.startswith("go ") and not go_version:
            value = line[3:].strip().split()[0] if line[3:].strip() else ""
            if value and len(value) <= 64:
                go_version = value
        elif line.startswith("toolchain ") and not toolchain:
            value = line[len("toolchain "):].strip().split()[0] if line[len("toolchain "):].strip() else ""
            if value and len(value) <= 128:
                toolchain = value
    return {
        "go_mod_present": True,
        "go_version": go_version,
        "toolchain": toolchain,
        "source": "go.mod text only",
    }


def _major_minor(version: str) -> str:
    match = re.search(r"(?:^|go)(\d+)\.(\d+)", str(version or ""))
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def attach_project_toolchain_context(result: dict[str, Any], project_meta: dict[str, Any] | None) -> dict[str, Any]:
    """Explain whether the local/pinned probe matches a project's declared Go line."""
    project_meta = dict(project_meta or {})
    if not project_meta:
        return result
    result["project_go"] = project_meta
    local = _major_minor(str(result.get("toolchain_version") or ""))
    declared = _major_minor(str(project_meta.get("toolchain") or project_meta.get("go_version") or ""))
    if not declared or not local:
        alignment = "unknown"
    elif declared == local:
        alignment = "major_minor_match"
    else:
        alignment = "major_minor_mismatch"
    result["toolchain_alignment"] = alignment
    op = str(result.get("operation") or "")
    if op in LANGUAGE_STABLE_OPERATIONS:
        result["applicability"] = "language_semantics_stable; the helper directly observes the language rule"
    elif alignment == "major_minor_match":
        result["applicability"] = "helper Go major.minor matches the project declaration; standard-library observation is directly relevant"
    elif op in STDLIB_OR_RUNTIME_OPERATIONS:
        result["applicability"] = "helper toolchain differs or is unknown; treat version-sensitive standard-library behavior as an observation, not proof of the target runtime"
    else:
        result["applicability"] = "toolchain applicability unknown"
    return result


def _normalize_inputs(op: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if op == "path_join":
        parts_raw = inputs.get("parts")
        if not isinstance(parts_raw, list) or not parts_raw or len(parts_raw) > 16:
            raise ValueError("path_join requires 1..16 string parts")
        return {"parts": [_bounded_string(item, "part") for item in parts_raw]}
    if op == "path_clean":
        return {"path": _bounded_string(inputs.get("path", ""), "path")}
    if op == "parse_duration":
        return {"duration": _bounded_string(inputs.get("duration", ""), "duration")}
    if op == "duration_multiply":
        value = _bounded_string(inputs.get("duration", ""), "duration")
        unit = _bounded_string(inputs.get("unit", "Millisecond"), "unit")
        allowed = {"Nanosecond", "Microsecond", "Millisecond", "Second", "Minute", "Hour"}
        if unit not in allowed:
            raise ValueError(f"unit must be one of {sorted(allowed)}")
        return {"duration": value, "unit": unit}
    if op == "failed_error_type_assertion":
        return {}
    if op == "strings_replace":
        value = _bounded_string(inputs.get("value", ""), "value")
        old = _bounded_string(inputs.get("old", ""), "old")
        new = _bounded_string(inputs.get("new", ""), "new")
        try:
            count = int(inputs.get("count", 1))
        except (ValueError, TypeError) as exc:
            raise ValueError(str(exc)) from exc
        if count < -1 or count > 1_000_000:
            raise ValueError("count is out of range")
        return {"value": value, "old": old, "new": new, "count": count}
    if op == "url_parse":
        return {"url": _bounded_string(inputs.get("url", ""), "url")}
    if op == "reverse_proxy_rewrite_headers":
        return {}
    raise ValueError("unsupported Go semantics operation")


def check_go_semantics(operation: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run an allow-listed fixed Go language/standard-library probe.

    Docker releases execute a small helper precompiled by the pinned Go builder
    stage. Source-tree development falls back to `go run` on the same fixed
    bundled helper source. Repository code is never compiled or executed.
    """
    op = str(operation or "").strip().lower().replace("-", "_")
    if op not in SUPPORTED_OPERATIONS:
        return {
            "status": "rejected",
            "operation": op,
            "reason": "unsupported Go semantics operation",
            "supported_operations": list(SUPPORTED_OPERATIONS),
        }
    try:
        normalized = _normalize_inputs(op, dict(inputs or {}))
    except ValueError as exc:
        return {"status": "rejected", "operation": op, "reason": str(exc)}
    result = _execute_probe(op, normalized)
    result["operation"] = op
    result["proof_scope"] = "allow-listed fixed Go language/standard-library helper; repository code was not executed"
    result["semantics_class"] = "language" if op in LANGUAGE_STABLE_OPERATIONS else "stdlib_or_runtime"
    return result
