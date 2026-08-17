#!/usr/bin/env python3
"""Portable and full Awoki runtime backup/restore support.

The archive format deliberately separates authoritative project/global data from
rebuildable indexes.  Portable backups exclude local FTS/Qdrant/OpenCode state;
full backups add the derived indexes and Qdrant storage.  Secrets and OpenCode
state require explicit opt-in.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

BACKUP_FORMAT = "awoki-runtime-backup"
BACKUP_SCHEMA_VERSION = 1
ARCHIVE_ROOT = "awoki-backup"
MANIFEST_MEMBER = f"{ARCHIVE_ROOT}/manifest.json"
PAYLOAD_ROOT = f"{ARCHIVE_ROOT}/payload"
LOCK_NAME = "backup-restore.lock"
KNOWN_PAYLOAD_ROLES = {
    "workspace",
    "harness_state",
    "harness_artifacts",
    "harness_memory",
    "harness_notes",
    "global_repo",
    "global_configured",
    "global_skills_configured",
    "harness_index",
    "qdrant",
    "opencode_state",
    "dotenv",
    "ssh_container",
}

FILE_PAYLOAD_ROLES = {"harness_notes", "dotenv"}

WORKSPACE_PLACEHOLDERS = {
    "README.md": "# Awoki workspace\n\nHuman-facing project workspace. Awoki core state lives in `.harness/`; global state lives in `.awoki-global/` or `/global` in the SSH container.\n\n- `projects/` — first-class project workspaces (`workspace/projects/<project_id>/`).\n- `notes/` — human notes you intentionally keep visible.\n- `corpora/` — documents/code/reports you may index intentionally.\n- `artifacts/` — project artifacts you choose to stage.\n- `reports/` — generated human reports.\n- `templates/` — reusable local templates.\n- `scratch/` — disposable local scratch space.\n",
    "notes/README.md": "# Workspace notes\n\nHuman-written notes. Project-specific thoughts go under `workspace/projects/<project_id>/notes/thoughts.md`.\n",
    "projects/README.md": "# Projects\n\nFirst-class Awoki project workspaces. Create one with `project_create(name)` or `.harness/project.py create NAME`.\n",
    "corpora/README.md": "# Corpora\n\nDocuments/code/reports staged for optional indexing. Do not place secrets here.\n",
    "artifacts/README.md": "# Workspace artifacts\n\nHuman-facing artifacts. Awoki managed artifacts live under `.harness/artifacts/`.\n",
    "reports/README.md": "# Reports\n\nGenerated reports or summaries intended for humans.\n",
    "templates/README.md": "# Templates\n\nReusable local templates.\n",
    "scratch/README.md": "# Scratch\n\nDisposable scratch space; do not rely on this for durable memory.\n",
}
GLOBAL_README_PLACEHOLDER = "# Awoki global state\n\nRepo-local global state for the OpenCode-over-SSH workflow. Inside the container this is mounted at `/global`.\n\n- `global/` — reusable global memories and logs.\n- `skills/` — optional global skills.\n- `state/burp/runs/` — global Burp evidence runs.\n- `state/registry/` — resource registry and context packs.\n"
HARNESS_NOTES_PLACEHOLDER = "# Local Awoki notes\n\nRuntime-only compatibility notes. Prefer `workspace/projects/<project_id>/notes/thoughts.md` for project continuity.\n"
TRACKED_RUNTIME_READMES = {
    "harness_state": {"README.md": None, LOCK_NAME: None, "layout_initialized.json": None},
    "harness_artifacts": {
        "README.md": None,
        "burp/README.md": None,
        "code/README.md": None,
        "docs/README.md": None,
        "evidence/README.md": None,
        "reports/README.md": None,
    },
    "harness_index": {"README.md": None},
}

CONFIG_KEYS = (
    "AWOKI_COMPOSE_PROJECT_NAME",
    "AWOKI_EMBEDDING_PROVIDER",
    "AWOKI_EMBEDDING_MODEL",
    "AWOKI_EMBEDDING_DEPLOYMENT_ID",
    "AWOKI_EMBEDDING_BASE_URL",
    "AWOKI_EMBEDDING_NORMALIZE",
    "AWOKI_VECTOR_SIZE",
    "AWOKI_QDRANT_COLLECTION",
    "AWOKI_CODE_QDRANT_COLLECTION",
    "AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH",
    "AWOKI_RERANK_ENABLED",
    "AWOKI_RERANK_PROVIDER",
    "AWOKI_RERANK_URL",
)


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class PayloadSpec:
    role: str
    source: Path
    archive_prefix: str
    restore_kind: str
    exclude: Callable[[PurePosixPath], bool]
    sensitive: bool = False


@dataclass(frozen=True)
class Entry:
    source: Path
    arcname: str
    size: int


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).rstrip()
        values[key] = value
    return values


def _expand_config_path(value: str, *, home: Path, base: Path) -> Path:
    value = value.replace("${HOME}", str(home)).replace("$HOME", str(home))
    if value.startswith("~/"):
        value = str(home / value[2:])
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _resolved_config(
    root: Path,
    *,
    dotenv_override: dict[str, str] | None = None,
    include_environment: bool = True,
) -> tuple[dict[str, str], set[str]]:
    file_values = _parse_dotenv(root / ".env")
    if dotenv_override is not None:
        file_values = dict(dotenv_override)
    explicit: set[str] = set(file_values)
    values = dict(file_values)
    if include_environment:
        for key in set(CONFIG_KEYS) | {
            "AWOKI_GLOBAL_ROOT", "HARNESS_GLOBAL_ROOT",
            "AWOKI_GLOBAL_SKILLS_DIR", "HARNESS_GLOBAL_SKILLS_DIR",
        }:
            if key in os.environ:
                values[key] = os.environ[key]
                explicit.add(key)
    return values, explicit


def _configured_roots(
    root: Path,
    *,
    dotenv_override: dict[str, str] | None = None,
    include_environment: bool = True,
) -> dict[str, Path]:
    values, explicit = _resolved_config(
        root, dotenv_override=dotenv_override, include_environment=include_environment
    )
    home = Path.home()
    global_value = values.get("AWOKI_GLOBAL_ROOT") or values.get("HARNESS_GLOBAL_ROOT")
    if global_value:
        configured_global = _expand_config_path(global_value, home=home, base=root)
    elif "AWOKI_GLOBAL_ROOT" in explicit or "HARNESS_GLOBAL_ROOT" in explicit:
        configured_global = root / ".awoki-global"
    else:
        configured_global = (home / ".awoki").resolve()
    skills_value = values.get("AWOKI_GLOBAL_SKILLS_DIR") or values.get("HARNESS_GLOBAL_SKILLS_DIR")
    configured_skills = (
        _expand_config_path(skills_value, home=home, base=root)
        if skills_value
        else (home / ".config" / "opencode" / "skills").resolve()
    )
    return {
        "repo_global": root / ".awoki-global",
        "configured_global": configured_global,
        "configured_skills": configured_skills,
    }


def _redact_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = hostname + port
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _nonsecret_config(root: Path) -> dict[str, str]:
    values, _ = _resolved_config(root)
    defaults = {
        "AWOKI_COMPOSE_PROJECT_NAME": "awoki",
        "AWOKI_EMBEDDING_PROVIDER": "openai",
        "AWOKI_EMBEDDING_MODEL": "text-embeddings-inference",
        "AWOKI_EMBEDDING_DEPLOYMENT_ID": "",
        "AWOKI_EMBEDDING_NORMALIZE": "1",
        "AWOKI_VECTOR_SIZE": "768",
        "AWOKI_QDRANT_COLLECTION": "awoki_jina_embeddings_v2_base_code_768",
        "AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH": "0",
        "AWOKI_RERANK_ENABLED": "0",
        "AWOKI_RERANK_PROVIDER": "tei",
    }
    result: dict[str, str] = {}
    for key in CONFIG_KEYS:
        value = values.get(key, defaults.get(key, ""))
        if key.endswith("_URL"):
            value = _redact_url(value)
        result[key] = value
    return result


def _git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    commit = run("rev-parse", "HEAD")
    branch = run("branch", "--show-current")
    status = run("status", "--porcelain")
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status),
        "dirty_entry_count": len(status.splitlines()) if status else 0,
    }


def _repository_portability_issues(root: Path) -> list[str]:
    workspace = (root / "workspace").resolve()
    projects = workspace / "projects"
    if not projects.exists():
        return []
    issues: list[str] = []
    git_markers: list[Path] = []
    for repo_root in projects.glob("*/repo"):
        if repo_root.is_dir():
            git_markers.extend(repo_root.rglob(".git"))
    for git_marker in sorted(set(git_markers)):
        repo = git_marker.parent
        git_dir = git_marker
        if git_marker.is_file():
            first = git_marker.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            if first and first[0].lower().startswith("gitdir:"):
                raw = first[0].split(":", 1)[1].strip()
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = repo / candidate
                git_dir = candidate.resolve(strict=False)
                try:
                    git_dir.relative_to(workspace)
                except ValueError:
                    issues.append(f"{git_marker} points to external Git metadata at {git_dir}")
        alternates = git_dir / "objects" / "info" / "alternates"
        if alternates.is_file():
            for raw in alternates.read_text(encoding="utf-8", errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = alternates.parent.parent / candidate
                resolved = candidate.resolve(strict=False)
                try:
                    resolved.relative_to(workspace)
                except ValueError:
                    issues.append(f"{alternates} references an external object store at {resolved}")
    return issues


def _qdrant_image_reference(root: Path) -> str:
    for filename in ("docker-compose.opencode.yml", "docker-compose.yml"):
        path = root / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?ms)^\s{2}qdrant:\s*$.*?^\s{4}image:\s*([^\s#]+)", text)
        if match:
            return match.group(1).strip("'\"")
    return ""


def _qdrant_image_identity(image: str) -> dict[str, Any]:
    result: dict[str, Any] = {"reference": image, "image_id": "", "repo_digests": []}
    if not image or shutil.which("docker") is None:
        return result
    completed = subprocess.run(
        ["docker", "image", "inspect", image], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        return result
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return result
    if rows and isinstance(rows[0], dict):
        result["image_id"] = str(rows[0].get("Id") or "")
        result["repo_digests"] = sorted(str(item) for item in rows[0].get("RepoDigests") or [])
    return result


def _running_compose_services(root: Path) -> list[dict[str, str]]:
    if shutil.which("docker") is None:
        return []
    version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        detail = version.stderr.strip() or version.stdout.strip() or "unknown Docker Compose error"
        raise BackupError(f"cannot verify Awoki container state because Docker Compose is unavailable: {detail}")
    running: list[dict[str, str]] = []
    for filename in ("docker-compose.yml", "docker-compose.opencode.yml"):
        if not (root / filename).exists():
            continue
        completed = subprocess.run(
            ["docker", "compose", "-f", filename, "ps", "--status", "running", "--services"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Docker Compose error"
            raise BackupError(
                f"cannot verify running services for {filename}: {detail}. "
                "Start/fix Docker and retry rather than assuming the runtime is stopped."
            )
        for service in completed.stdout.splitlines():
            service = service.strip()
            if service:
                running.append({"compose_file": filename, "service": service})
    return running


def _stop_compose(root: Path, running: list[dict[str, str]]) -> list[str]:
    files = sorted({row["compose_file"] for row in running})
    stopped: list[str] = []
    for filename in files:
        completed = subprocess.run(
            ["docker", "compose", "-f", filename, "down"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise BackupError(f"failed to stop services from {filename}: {detail}")
        stopped.append(filename)
    return stopped


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _check_operation_lock(root: Path, *, recover_stale: bool = True) -> dict[str, Any]:
    lock = root / ".harness" / "state" / LOCK_NAME
    try:
        st = lock.lstat()
    except FileNotFoundError:
        return {"status": "clear", "lock": str(lock)}
    if not stat.S_ISREG(st.st_mode):
        raise BackupError(f"unsafe backup/restore lock is not a regular file: {lock}")
    owner = lock.read_text(encoding="utf-8", errors="replace")[:4096].strip()
    match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", owner)
    pid = int(match.group(1)) if match else -1
    if pid > 0 and not _process_is_alive(pid) and recover_stale:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        return {"status": "stale_lock_removed", "lock": str(lock), "pid": pid}
    raise BackupError(f"another backup/restore operation is active ({lock}: {owner or 'unknown'})")


@contextlib.contextmanager
def _operation_lock(root: Path) -> Iterator[None]:
    state = root / ".harness" / "state"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / LOCK_NAME
    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            try:
                result = _check_operation_lock(root, recover_stale=True)
            except BackupError as check_exc:
                raise check_exc from exc
            if result["status"] == "stale_lock_removed":
                continue
            raise BackupError(f"could not acquire backup/restore lock: {lock}") from exc
    else:
        raise BackupError(f"could not acquire backup/restore lock: {lock}")
    try:
        try:
            os.write(fd, f"pid={os.getpid()} created={_utc_now()}\n".encode())
        finally:
            os.close(fd)
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def _portable_workspace_exclude(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "index":
        return True
    if len(parts) >= 2 and parts[0] == ".lavish" and parts[1] == "state":
        return True
    return False


def _full_workspace_exclude(relative: PurePosixPath) -> bool:
    parts = relative.parts
    return len(parts) >= 2 and parts[0] == ".lavish" and parts[1] == "state"


def _portable_global_exclude(relative: PurePosixPath) -> bool:
    path = relative.as_posix()
    return path in {
        "global/index-manifest.json",
        "global/awoki_global_fts.sqlite",
        "global/awoki_global_fts.sqlite-wal",
        "global/awoki_global_fts.sqlite-shm",
    }


def _harness_state_exclude(relative: PurePosixPath) -> bool:
    return relative.as_posix() in {LOCK_NAME, "layout_initialized.json", "README.md"}


def _harness_artifacts_exclude(relative: PurePosixPath) -> bool:
    return relative.as_posix() in TRACKED_RUNTIME_READMES["harness_artifacts"]


def _harness_index_exclude(relative: PurePosixPath) -> bool:
    return relative.as_posix() == "README.md"


def _opencode_exclude(relative: PurePosixPath) -> bool:
    return bool(relative.parts and relative.parts[0] in {"cache", "npm"})


def _never_exclude(relative: PurePosixPath) -> bool:
    return False


def _validate_source_symlink(path: Path, relative: PurePosixPath) -> None:
    target_raw = os.readlink(path)
    target = PurePosixPath(target_raw)
    if target.is_absolute():
        raise BackupError(f"absolute symlink is not portable: {path} -> {target_raw}")
    normalized = PurePosixPath(os.path.normpath(str(relative.parent / target)))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise BackupError(f"symlink escapes archived payload root: {path} -> {target_raw}")


def _build_payload_specs(
    root: Path,
    *,
    mode: str,
    include_opencode_state: bool,
    include_secrets: bool,
) -> list[PayloadSpec]:
    roots = _configured_roots(root)
    specs = [
        PayloadSpec(
            "workspace", root / "workspace", f"{PAYLOAD_ROOT}/workspace", "repo:workspace",
            _portable_workspace_exclude if mode == "portable" else _full_workspace_exclude,
        ),
        PayloadSpec("harness_state", root / ".harness" / "state", f"{PAYLOAD_ROOT}/harness_state", "repo:.harness/state", _harness_state_exclude),
        PayloadSpec("harness_artifacts", root / ".harness" / "artifacts", f"{PAYLOAD_ROOT}/harness_artifacts", "repo:.harness/artifacts", _harness_artifacts_exclude),
        PayloadSpec("harness_memory", root / ".harness" / "memory", f"{PAYLOAD_ROOT}/harness_memory", "repo:.harness/memory", _never_exclude),
        PayloadSpec("harness_notes", root / ".harness" / "notes.md", f"{PAYLOAD_ROOT}/harness_notes", "repo:.harness/notes.md", _never_exclude),
        PayloadSpec(
            "global_repo", roots["repo_global"], f"{PAYLOAD_ROOT}/global_repo", "repo:.awoki-global",
            _portable_global_exclude if mode == "portable" else _never_exclude,
        ),
    ]
    if roots["configured_global"] != roots["repo_global"]:
        specs.append(PayloadSpec(
            "global_configured", roots["configured_global"], f"{PAYLOAD_ROOT}/global_configured", "configured:global_root",
            _portable_global_exclude if mode == "portable" else _never_exclude,
        ))
    # Avoid duplicating skills already contained by either global root.
    skills = roots["configured_skills"]
    contained = any(
        spec.source == skills or spec.source in skills.parents
        for spec in specs if spec.role.startswith("global_")
    )
    if not contained:
        specs.append(PayloadSpec(
            "global_skills_configured", skills, f"{PAYLOAD_ROOT}/global_skills_configured", "configured:global_skills",
            _never_exclude,
        ))
    if mode == "full":
        specs.extend([
            PayloadSpec("harness_index", root / ".harness" / "index", f"{PAYLOAD_ROOT}/harness_index", "repo:.harness/index", _harness_index_exclude),
            PayloadSpec("qdrant", root / "data" / "qdrant", f"{PAYLOAD_ROOT}/qdrant", "repo:data/qdrant", _never_exclude),
        ])
    if include_opencode_state:
        specs.append(PayloadSpec(
            "opencode_state", root / ".opencode-state", f"{PAYLOAD_ROOT}/opencode_state", "repo:.opencode-state",
            _opencode_exclude, sensitive=True,
        ))
    if include_secrets:
        specs.extend([
            PayloadSpec("dotenv", root / ".env", f"{PAYLOAD_ROOT}/dotenv", "repo:.env", _never_exclude, sensitive=True),
            PayloadSpec("ssh_container", root / ".ssh-container", f"{PAYLOAD_ROOT}/ssh_container", "repo:.ssh-container", _never_exclude, sensitive=True),
        ])
    mandatory_roles = {
        "workspace", "harness_state", "harness_artifacts", "harness_memory",
        "harness_notes", "global_repo",
    }
    if mode == "full":
        mandatory_roles.update({"harness_index", "qdrant"})
    # Missing optional external roots are omitted. Standard repo-local roles stay
    # in the manifest even when empty so restore semantics remain explicit.
    return [spec for spec in specs if spec.source.exists() or spec.role in mandatory_roles]


def _iter_entries(spec: PayloadSpec) -> Iterator[Entry]:
    source = spec.source
    if not source.exists() and not source.is_symlink():
        return
    if source.is_file() or source.is_symlink():
        st = source.lstat()
        if stat.S_ISLNK(st.st_mode):
            _validate_source_symlink(source, PurePosixPath(source.name))
        size = st.st_size if stat.S_ISREG(st.st_mode) else 0
        yield Entry(source, spec.archive_prefix, size)
        return

    # Add the root directory even when empty.
    yield Entry(source, spec.archive_prefix, 0)
    stack: list[tuple[Path, PurePosixPath]] = [(source, PurePosixPath())]
    while stack:
        directory, relative_dir = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except PermissionError as exc:
            raise BackupError(f"cannot read {directory}; stop services and repair permissions before backup") from exc
        directories: list[tuple[Path, PurePosixPath]] = []
        for child in children:
            relative = relative_dir / child.name
            if spec.exclude(relative):
                continue
            child_path = Path(child.path)
            try:
                st = child_path.lstat()
            except OSError as exc:
                raise BackupError(f"cannot stat {child_path}: {exc}") from exc
            mode = st.st_mode
            arcname = f"{spec.archive_prefix}/{relative.as_posix()}"
            if stat.S_ISDIR(mode):
                yield Entry(child_path, arcname, 0)
                directories.append((child_path, relative))
            elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                if stat.S_ISLNK(mode):
                    _validate_source_symlink(child_path, relative)
                yield Entry(child_path, arcname, st.st_size if stat.S_ISREG(mode) else 0)
            else:
                raise BackupError(
                    f"unsupported socket/device/FIFO in archived data: {child_path}; "
                    "stop the owning process or remove the runtime-only file before backup"
                )
        stack.extend(reversed(directories))


def _archive_member_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    # Preserve executable/readability semantics but never archive setuid/setgid/sticky bits.
    info.mode &= 0o0777
    return info


def _write_json_member(tar: tarfile.TarFile, name: str, data: dict[str, Any]) -> None:
    raw = (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    info.mode = 0o600
    info.mtime = int(dt.datetime.now(dt.timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(raw))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksum(path: Path, digest: str) -> Path:
    sidecar = Path(str(path) + ".sha256")
    temp = sidecar.with_name(sidecar.name + ".tmp")
    temp.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(sidecar)
    return sidecar


def create_backup(
    root: Path,
    *,
    mode: str,
    output_dir: Path,
    include_opencode_state: bool = False,
    include_secrets: bool = False,
    allow_live: bool = False,
    stop_containers: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    if mode not in {"portable", "full"}:
        raise BackupError(f"unsupported backup mode: {mode}")
    if not (root / ".harness").is_dir():
        raise BackupError(f"not an Awoki root: {root}")
    repository_issues = _repository_portability_issues(root)
    if repository_issues:
        raise BackupError(
            "workspace repositories depend on Git metadata/object stores outside the archived workspace; "
            "convert linked worktrees/alternates to self-contained clones before backup:\n  "
            + "\n  ".join(repository_issues)
        )
    output_dir = output_dir.expanduser().resolve()
    if output_dir == root or root in output_dir.parents:
        raise BackupError("backup output directory must be outside the Awoki repository")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"awoki-{mode}-{_timestamp_slug()}.tar.gz"
    if archive.exists():
        raise BackupError(f"backup output already exists: {archive}")

    with _operation_lock(root):
        running = _running_compose_services(root)
        stopped: list[str] = []
        if running and stop_containers:
            stopped = _stop_compose(root, running)
            running = _running_compose_services(root)
        qdrant_running = any(row.get("service") == "qdrant" for row in running)
        if mode == "full" and running:
            details = ", ".join(f"{row['compose_file']}:{row['service']}" for row in running)
            if qdrant_running:
                reason = "Qdrant is running and raw Qdrant storage may never be copied live"
            else:
                reason = "full backups include mutable derived indexes and require complete quiescence"
            raise BackupError(
                f"{reason} ({details}). Use --stop-containers or stop both Compose projects first."
            )
        if running and not allow_live:
            details = ", ".join(f"{row['compose_file']}:{row['service']}" for row in running)
            raise BackupError(
                "Awoki containers are running; stop them first for a consistent backup "
                f"({details}), or explicitly use --stop-containers/--allow-live"
            )

        specs = _build_payload_specs(
            root,
            mode=mode,
            include_opencode_state=include_opencode_state,
            include_secrets=include_secrets,
        )
        for spec in specs:
            if spec.source.is_dir() and (output_dir == spec.source or spec.source in output_dir.parents):
                raise BackupError(
                    f"backup output directory {output_dir} is inside archived source {spec.source}"
                )
        entries_by_role: dict[str, list[Entry]] = {}
        payload_rows: list[dict[str, Any]] = []
        for spec in specs:
            entries = list(_iter_entries(spec))
            entries_by_role[spec.role] = entries
            payload_rows.append({
                "role": spec.role,
                "archive_prefix": spec.archive_prefix,
                "restore_kind": spec.restore_kind,
                "source": str(spec.source),
                "sensitive": spec.sensitive,
                "entry_count": len(entries),
                "regular_file_bytes": sum(entry.size for entry in entries),
            })

        qdrant_image = _qdrant_image_reference(root)
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at_utc": _utc_now(),
            "mode": mode,
            "root_name": root.name,
            "source": {
                "git": _git_metadata(root),
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "user": getpass.getuser(),
            },
            "configuration": _nonsecret_config(root),
            "qdrant_image": _qdrant_image_identity(qdrant_image),
            "payloads": payload_rows,
            "security": {
                "contains_explicit_secrets": include_secrets,
                "contains_opencode_state": include_opencode_state,
                "archive_permissions": "0600",
                "note": "Project/global data may itself be sensitive even when .env, SSH keys, and OpenCode state are excluded.",
            },
            "consistency": {
                "containers_running_at_capture": running,
                "compose_files_stopped_by_command": stopped,
                "live_capture_explicitly_allowed": allow_live,
            },
            "restore": {
                "portable_reindex_default": "lexical",
                "full_reindex_default": "none",
                "named_docker_volumes_included": False,
                "named_volume_note": "Neovim state and SSH server host-key volumes are recreated by the new installation.",
            },
        }

        temp = archive.with_name(archive.name + ".tmp")
        try:
            with tarfile.open(temp, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
                _write_json_member(tar, MANIFEST_MEMBER, manifest)
                for spec in specs:
                    for entry in entries_by_role[spec.role]:
                        try:
                            # Avoid hard-link members without dereferencing symlinks.
                            # Each entry is added independently, so resetting inode
                            # tracking stores regular files normally while preserving
                            # safe relative symlinks as links.
                            tar.inodes.clear()
                            tar.add(entry.source, arcname=entry.arcname, recursive=False, filter=_archive_member_filter)
                        except PermissionError as exc:
                            raise BackupError(
                                f"cannot read {entry.source}; stop containers and repair ownership before retrying"
                            ) from exc
            post_capture_running = _running_compose_services(root)
            if post_capture_running and (mode == "full" or not allow_live):
                details = ", ".join(
                    f"{row['compose_file']}:{row['service']}" for row in post_capture_running
                )
                raise BackupError(
                    "Awoki services started during backup, so the capture was discarded as potentially inconsistent "
                    f"({details})"
                )
            os.chmod(temp, 0o600)
            temp.replace(archive)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    digest = _sha256(archive)
    sidecar = _write_checksum(archive, digest)
    try:
        verified = verify_backup(archive, require_checksum=True)
    except BackupError:
        with contextlib.suppress(FileNotFoundError):
            archive.unlink()
        with contextlib.suppress(FileNotFoundError):
            sidecar.unlink()
        raise
    return {
        "status": "created",
        "mode": mode,
        "archive": str(archive),
        "checksum_file": str(sidecar),
        "sha256": verified["sha256"],
        "verified": True,
        "payload_count": len(payload_rows),
        "entry_count": sum(row["entry_count"] for row in payload_rows),
        "regular_file_bytes": sum(row["regular_file_bytes"] for row in payload_rows),
        "contains_explicit_secrets": include_secrets,
        "contains_opencode_state": include_opencode_state,
        "containers_stopped": stopped,
    }


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    rows = manifest.get("payloads")
    if not isinstance(rows, list):
        raise BackupError("backup manifest payloads must be a list")
    roles: set[str] = set()
    prefixes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise BackupError("backup manifest contains a non-object payload row")
        role = str(row.get("role") or "")
        prefix = str(row.get("archive_prefix") or "")
        if role not in KNOWN_PAYLOAD_ROLES:
            raise BackupError(f"backup manifest contains unknown payload role: {role!r}")
        expected_prefix = f"{PAYLOAD_ROOT}/{role}"
        if prefix != expected_prefix:
            raise BackupError(
                f"backup payload prefix mismatch for {role}: expected {expected_prefix}, got {prefix!r}"
            )
        if role in roles or prefix in prefixes:
            raise BackupError(f"backup manifest contains duplicate payload role/prefix: {role}")
        roles.add(role)
        prefixes.add(prefix)
        for key in ("entry_count", "regular_file_bytes"):
            value = row.get(key)
            if not isinstance(value, int) or value < 0:
                raise BackupError(f"backup payload {role} has invalid {key}: {value!r}")
    required = {
        "workspace", "harness_state", "harness_artifacts", "harness_memory",
        "harness_notes", "global_repo",
    }
    if manifest.get("mode") == "full":
        required.update({"harness_index", "qdrant"})
    elif roles & {"harness_index", "qdrant"}:
        raise BackupError("portable backup manifest may not contain full-only index/Qdrant payloads")
    missing = sorted(required - roles)
    if missing:
        raise BackupError(f"backup manifest is missing required payloads: {missing}")


def _payload_member_is_excluded(mode: str, role: str, suffix: PurePosixPath) -> bool:
    if role == "workspace":
        return (_portable_workspace_exclude if mode == "portable" else _full_workspace_exclude)(suffix)
    if role in {"global_repo", "global_configured"} and mode == "portable":
        return _portable_global_exclude(suffix)
    if role == "harness_state":
        return _harness_state_exclude(suffix)
    if role == "harness_artifacts":
        return _harness_artifacts_exclude(suffix)
    if role == "harness_index":
        return _harness_index_exclude(suffix)
    if role == "opencode_state":
        return _opencode_exclude(suffix)
    return False


def _read_manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        member = tar.getmember(MANIFEST_MEMBER)
    except KeyError as exc:
        raise BackupError(f"archive is missing {MANIFEST_MEMBER}") from exc
    _validate_member(member)
    if not member.isfile():
        raise BackupError("backup manifest must be a regular file")
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError("backup manifest is not a regular file")
    try:
        manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BackupError(f"invalid backup manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must be an object")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(f"unsupported archive format: {manifest.get('format')!r}")
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError(f"unsupported backup schema version: {manifest.get('schema_version')!r}")
    if manifest.get("mode") not in {"portable", "full"}:
        raise BackupError(f"invalid backup mode: {manifest.get('mode')!r}")
    _validate_manifest_shape(manifest)
    return manifest


def _validate_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise BackupError(f"unsafe archive path: {member.name}")
    if not member.name.startswith(f"{ARCHIVE_ROOT}/"):
        raise BackupError(f"archive member is outside {ARCHIVE_ROOT}: {member.name}")
    if member.ischr() or member.isblk() or member.isfifo():
        raise BackupError(f"special archive member is not allowed: {member.name}")
    if member.islnk():
        raise BackupError(f"hard-link archive member is not allowed: {member.name}")
    if not (member.isfile() or member.isdir() or member.issym()):
        raise BackupError(f"unsupported archive member type: {member.name}")
    if member.issym():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            raise BackupError(f"absolute link target is not allowed: {member.name} -> {member.linkname}")
        parts = path.parts
        if len(parts) < 4 or parts[0] != ARCHIVE_ROOT or parts[1] != "payload":
            raise BackupError(f"link is outside a payload root: {member.name}")
        relative = PurePosixPath(*parts[3:])
        normalized = PurePosixPath(os.path.normpath(str(relative.parent / target)))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise BackupError(f"link escapes payload root: {member.name} -> {member.linkname}")


def _expected_checksum(archive: Path) -> str | None:
    sidecar = Path(str(archive) + ".sha256")
    if not sidecar.exists():
        return None
    line = sidecar.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
    match = re.match(r"^([0-9a-fA-F]{64})(?:\s+\*?.*)?$", line)
    if not match:
        raise BackupError(f"invalid checksum file: {sidecar}")
    return match.group(1).lower()


def verify_backup(archive: Path, *, require_checksum: bool = True) -> dict[str, Any]:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise BackupError(f"backup archive not found: {archive}")
    expected = _expected_checksum(archive)
    if require_checksum and expected is None:
        raise BackupError(f"checksum sidecar is missing: {archive}.sha256")
    actual = _sha256(archive)
    if expected and actual != expected:
        raise BackupError(f"SHA-256 mismatch for {archive.name}: expected {expected}, got {actual}")
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            manifest = _read_manifest(tar)
            members = tar.getmembers()
            names: set[str] = set()
            rows = [row for row in manifest.get("payloads") or [] if isinstance(row, dict)]
            prefixes = {str(row["archive_prefix"]): row for row in rows}
            actual_counts = {prefix: 0 for prefix in prefixes}
            actual_bytes = {prefix: 0 for prefix in prefixes}
            exact_members: dict[str, tarfile.TarInfo] = {}
            manifest_count = 0
            for member in members:
                _validate_member(member)
                if member.name in names:
                    raise BackupError(f"archive contains duplicate member name: {member.name}")
                names.add(member.name)
                if member.name == MANIFEST_MEMBER:
                    manifest_count += 1
                    continue
                matched = next((prefix for prefix in prefixes if member.name == prefix or member.name.startswith(prefix + "/")), None)
                if matched is None:
                    raise BackupError(f"archive contains undeclared member: {member.name}")
                suffix_text = member.name[len(matched):].lstrip("/")
                if suffix_text and _payload_member_is_excluded(
                    str(manifest["mode"]), str(prefixes[matched]["role"]), PurePosixPath(suffix_text)
                ):
                    raise BackupError(
                        f"archive contains a member excluded by {manifest['mode']} policy: {member.name}"
                    )
                if member.name == matched:
                    exact_members[matched] = member
                actual_counts[matched] += 1
                if member.isfile():
                    actual_bytes[matched] += int(member.size)
            if manifest_count != 1:
                raise BackupError(f"archive must contain exactly one manifest, found {manifest_count}")
            for prefix, row in prefixes.items():
                if actual_counts[prefix] != int(row["entry_count"]):
                    raise BackupError(
                        f"payload entry count mismatch for {row['role']}: "
                        f"manifest={row['entry_count']} archive={actual_counts[prefix]}"
                    )
                if actual_bytes[prefix] != int(row["regular_file_bytes"]):
                    raise BackupError(
                        f"payload byte count mismatch for {row['role']}: "
                        f"manifest={row['regular_file_bytes']} archive={actual_bytes[prefix]}"
                    )
                role = str(row["role"])
                count = actual_counts[prefix]
                root_member = exact_members.get(prefix)
                if role in FILE_PAYLOAD_ROLES:
                    if count not in {0, 1} or (count == 1 and (root_member is None or not root_member.isfile())):
                        raise BackupError(f"file payload {role} must be empty or one regular file at {prefix}")
                elif count > 0 and (root_member is None or not root_member.isdir()):
                    raise BackupError(f"directory payload {role} must have a directory root at {prefix}")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"invalid or unreadable backup archive: {exc}") from exc
    return {
        "status": "verified",
        "archive": str(archive),
        "sha256": actual,
        "checksum_present": expected is not None,
        "mode": manifest["mode"],
        "created_at_utc": manifest.get("created_at_utc"),
        "member_count": len(members),
        "manifest": manifest,
    }


def inspect_backup(archive: Path) -> dict[str, Any]:
    result = verify_backup(archive, require_checksum=False)
    return {
        "status": "inspected",
        "archive": result["archive"],
        "sha256": result["sha256"],
        "checksum_present": result["checksum_present"],
        "member_count": result["member_count"],
        "manifest": result["manifest"],
    }


def _archive_dotenv(tar: tarfile.TarFile, manifest: dict[str, Any]) -> dict[str, str] | None:
    rows = manifest.get("payloads") or []
    row = next((item for item in rows if isinstance(item, dict) and item.get("role") == "dotenv"), None)
    if not row:
        return None
    prefix = str(row.get("archive_prefix") or "")
    try:
        member = tar.getmember(prefix)
    except KeyError:
        return None
    handle = tar.extractfile(member)
    if handle is None:
        return None
    raw = handle.read().decode("utf-8", errors="replace")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as temp:
        temp.write(raw)
        name = temp.name
    try:
        return _parse_dotenv(Path(name))
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(name).unlink()


def _restore_targets(root: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    roots = _configured_roots(root)
    return {
        "workspace": root / "workspace",
        "harness_state": root / ".harness" / "state",
        "harness_artifacts": root / ".harness" / "artifacts",
        "harness_memory": root / ".harness" / "memory",
        "harness_notes": root / ".harness" / "notes.md",
        "global_repo": roots["repo_global"],
        "global_configured": roots["configured_global"],
        "global_skills_configured": roots["configured_skills"],
        "harness_index": root / ".harness" / "index",
        "qdrant": root / "data" / "qdrant",
        "opencode_state": root / ".opencode-state",
        "dotenv": root / ".env",
        "ssh_container": root / ".ssh-container",
    }


def _resolved_top_level_aliases(anchor: Path) -> set[Path]:
    """Return canonical paths represented by immediate filesystem children."""
    try:
        children = list(anchor.iterdir())
    except OSError:
        return set()
    aliases: set[Path] = set()
    for child in children:
        try:
            aliases.add(child.resolve(strict=False))
        except (OSError, RuntimeError):
            continue
    return aliases


def _validate_restore_targets(
    root: Path,
    rows: list[dict[str, Any]],
    targets: dict[str, Path],
) -> None:
    root = root.resolve()
    home = Path.home().resolve()
    selected: list[tuple[str, Path]] = []
    for row in rows:
        role = str(row.get("role") or "")
        target = targets.get(role)
        if target is None:
            raise BackupError(f"unknown payload role in archive: {role}")

        # Keep both path forms.  macOS exposes aliases such as /etc ->
        # /private/etc and /var -> /private/var.  Looking only at the resolved
        # path can make a filesystem-top-level target appear one component
        # deeper and accidentally bypass the broad-target guard.
        logical = target.expanduser()
        if not logical.is_absolute():
            logical = root / logical
        logical = Path(os.path.abspath(logical))
        absolute = logical.resolve(strict=False)
        anchor = Path(absolute.anchor)
        logical_anchor = Path(logical.anchor)
        if absolute == anchor or absolute == root or absolute in root.parents:
            raise BackupError(f"unsafe restore target for {role}: {absolute}")
        if role in {"global_configured", "global_skills_configured"}:
            if absolute == home:
                raise BackupError(f"configured restore target may not be the home directory itself: {role} -> {absolute}")
            if len(logical.parts) < 3 or len(absolute.parts) < 3:
                raise BackupError(
                    f"configured restore target is too broad for safe replacement: {role} -> {logical}"
                )
            # Also reject a symlink alias of an immediate filesystem child.
            # This catches targets such as /private/etc when /etc resolves to it.
            if absolute in _resolved_top_level_aliases(logical_anchor):
                raise BackupError(
                    f"configured restore target is too broad for safe replacement: {role} -> {logical}"
                )
        selected.append((role, absolute))
    for index, (left_role, left) in enumerate(selected):
        for right_role, right in selected[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise BackupError(
                    "restore payload targets overlap; adjust destination AWOKI_GLOBAL_ROOT/"
                    f"AWOKI_GLOBAL_SKILLS_DIR before restoring: {left_role}={left}, {right_role}={right}"
                )


def _meaningful_entries(
    path: Path,
    *,
    placeholder_text: str | None = None,
    allow_any_existing: bool = False,
) -> list[Path]:
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        return [path]
    if path.is_file():
        if path.stat().st_size == 0:
            return []
        if allow_any_existing:
            return []
        if placeholder_text is not None:
            try:
                if path.read_text(encoding="utf-8") == placeholder_text:
                    return []
            except (OSError, UnicodeError):
                pass
        return [path]
    return [child for child in path.iterdir()]

def _clear_target(
    path: Path,
    *,
    preserve_names: set[str] | None = None,
    preserve_relative_paths: set[str] | None = None,
    preserve_top_level: set[str] | None = None,
) -> None:
    preserve_names = preserve_names or set()
    preserve_relative_paths = preserve_relative_paths or set()
    preserve_top_level = preserve_top_level or set()
    if not path.exists() and not path.is_symlink():
        return
    if path.is_file() or path.is_symlink():
        if path.name not in preserve_names:
            path.unlink()
        return
    children = sorted(path.rglob("*"), key=lambda item: len(item.relative_to(path).parts), reverse=True)
    for child in children:
        relative = child.relative_to(path)
        relative_text = relative.as_posix()
        if relative.parts and relative.parts[0] in preserve_top_level:
            continue
        if (
            child.name in preserve_names
            and len(relative.parts) == 1
            and child.is_file()
            and not child.is_symlink()
        ):
            continue
        if relative_text in preserve_relative_paths and child.is_file() and not child.is_symlink():
            continue
        if child.is_dir() and not child.is_symlink():
            with contextlib.suppress(OSError):
                child.rmdir()
        else:
            child.unlink()

def _full_compatibility_issues(root: Path, manifest: dict[str, Any]) -> list[str]:
    source = manifest.get("configuration") or {}
    destination = _nonsecret_config(root)
    issues: list[str] = []
    for key in (
        "AWOKI_VECTOR_SIZE",
        "AWOKI_QDRANT_COLLECTION",
        "AWOKI_CODE_QDRANT_COLLECTION",
        "AWOKI_EMBEDDING_PROVIDER",
        "AWOKI_EMBEDDING_MODEL",
        "AWOKI_EMBEDDING_NORMALIZE",
    ):
        old = str(source.get(key) or "")
        new = str(destination.get(key) or "")
        if old and new and old != new:
            issues.append(f"{key} differs (backup={old!r}, destination={new!r})")
    qdrant_payload = next(
        (
            row
            for row in manifest.get("payloads") or []
            if isinstance(row, dict) and row.get("role") == "qdrant"
        ),
        {},
    )
    if int(qdrant_payload.get("regular_file_bytes") or 0) > 0:
        old_deployment = str(source.get("AWOKI_EMBEDDING_DEPLOYMENT_ID") or "")
        new_deployment = str(destination.get("AWOKI_EMBEDDING_DEPLOYMENT_ID") or "")
        if not old_deployment or not new_deployment:
            issues.append(
                "actual remote embedding deployment identity is not configured on both installations; "
                "set AWOKI_EMBEDDING_DEPLOYMENT_ID to the served model/revision or use a portable restore"
            )
        elif old_deployment != new_deployment:
            issues.append(
                "AWOKI_EMBEDDING_DEPLOYMENT_ID differs "
                f"(backup={old_deployment!r}, destination={new_deployment!r})"
            )
    source_image = manifest.get("qdrant_image") or {}
    destination_image = _qdrant_image_identity(_qdrant_image_reference(root))
    old_reference = str(source_image.get("reference") or "")
    new_reference = str(destination_image.get("reference") or "")
    if old_reference and new_reference and old_reference != new_reference:
        issues.append(
            f"Qdrant image reference differs (backup={old_reference!r}, destination={new_reference!r})"
        )
    old_digests = set(source_image.get("repo_digests") or [])
    new_digests = set(destination_image.get("repo_digests") or [])
    if old_digests and new_digests and old_digests.isdisjoint(new_digests):
        issues.append("Qdrant image digest differs between backup and destination")
    old_id = str(source_image.get("image_id") or "")
    new_id = str(destination_image.get("image_id") or "")
    immutable_reference = "@sha256:" in old_reference and old_reference == new_reference
    identity_matches = bool(
        immutable_reference
        or (old_digests and new_digests and not old_digests.isdisjoint(new_digests))
        or (old_id and new_id and old_id == new_id)
    )
    if (old_reference.endswith(":latest") or new_reference.endswith(":latest")) and not identity_matches:
        issues.append(
            "Qdrant uses the mutable :latest tag and matching image identity could not be proven; "
            "pull/inspect the same image on both installations or use a portable restore"
        )
    return issues


def _safe_staging_destination(role_root: Path, suffix: str) -> Path:
    relative = PurePosixPath(suffix)
    if relative.is_absolute() or ".." in relative.parts:
        raise BackupError(f"unsafe payload path: {suffix}")
    destination = role_root.joinpath(*relative.parts) if relative.parts else role_root
    current = destination.parent
    while current != role_root.parent and current != current.parent:
        if current.is_symlink():
            raise BackupError(f"payload member descends through a symlink: {suffix}")
        current = current.parent
    try:
        destination.resolve(strict=False).relative_to(role_root.parent.resolve())
    except ValueError as exc:
        raise BackupError(f"payload path escapes staging root: {suffix}") from exc
    return destination


def _extract_payload_to_staging(
    tar: tarfile.TarFile,
    manifest: dict[str, Any],
    staging: Path,
) -> dict[str, Path]:
    roles: dict[str, Path] = {}
    members = tar.getmembers()
    for row in manifest.get("payloads") or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "")
        prefix = str(row.get("archive_prefix") or "")
        if not role or not prefix:
            raise BackupError("backup manifest contains an invalid payload row")
        role_root = staging / role
        matched = False
        for member in members:
            if member.name != prefix and not member.name.startswith(prefix + "/"):
                continue
            _validate_member(member)
            matched = True
            suffix = member.name[len(prefix):].lstrip("/")
            destination = _safe_staging_destination(role_root, suffix)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                os.chmod(destination, member.mode & 0o777)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise BackupError(f"absolute symlink is not allowed: {member.name}")
                normalized = PurePosixPath(os.path.normpath(str(PurePosixPath(suffix).parent / target)))
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise BackupError(f"symlink escapes payload root: {member.name} -> {member.linkname}")
                os.symlink(member.linkname, destination)
                continue
            if member.islnk():
                raise BackupError(f"hard-link archive member is not supported: {member.name}")
            if not member.isfile():
                raise BackupError(f"unsupported archive member: {member.name}")
            source = tar.extractfile(member)
            if source is None:
                raise BackupError(f"cannot read archive member: {member.name}")
            with destination.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(destination, member.mode & 0o777)
            with contextlib.suppress(OSError):
                os.utime(destination, (member.mtime, member.mtime), follow_symlinks=False)
        if matched:
            roles[role] = role_root
    return roles


def _apply_staged_payload(staged: Path, target: Path) -> None:
    if staged.is_file() or staged.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(staged), str(target))
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in list(staged.iterdir()):
        destination = target / child.name
        if child.is_dir() and not child.is_symlink():
            _apply_staged_payload(child, destination)
            with contextlib.suppress(OSError):
                child.rmdir()
        else:
            _apply_staged_payload(child, destination)

def _invalidate_portable_indexes(root: Path, targets: dict[str, Path], *, force: bool) -> None:
    derived = [
        root / ".harness" / "index",
        root / "data" / "qdrant",
    ]
    for role in ("global_repo", "global_configured"):
        global_root = targets.get(role)
        if global_root:
            derived.extend([
                global_root / "global" / "awoki_global_fts.sqlite",
                global_root / "global" / "awoki_global_fts.sqlite-wal",
                global_root / "global" / "awoki_global_fts.sqlite-shm",
                global_root / "global" / "index-manifest.json",
            ])
    conflicts: list[str] = []
    unique_derived: list[Path] = []
    seen: set[Path] = set()
    for path in derived:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_derived.append(path)
    for path in unique_derived:
        if path.is_dir():
            meaningful = _tree_conflicts(path, allowed_placeholders={"README.md": None})
            conflicts.extend(str(item) for item in meaningful)
        elif path.exists() or path.is_symlink():
            conflicts.append(str(path))
    if conflicts and not force:
        preview = "\n  ".join(conflicts[:12])
        raise BackupError(
            "portable restore found existing derived indexes that could reference different data; "
            "use --force to clear them before restore:\n  " + preview
        )
    if force:
        _clear_target(root / ".harness" / "index", preserve_names={"README.md"})
        _clear_target(root / "data" / "qdrant")
        for path in unique_derived:
            if path in {root / ".harness" / "index", root / "data" / "qdrant"}:
                continue
            if path.exists() or path.is_symlink():
                path.unlink()


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path.resolve(strict=False)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _check_restore_disk_space(
    root: Path,
    rows: list[dict[str, Any]],
    targets: dict[str, Path],
) -> None:
    margin = 64 * 1024 * 1024
    total_bytes = sum(int(row.get("regular_file_bytes") or 0) for row in rows)
    staging_ancestor = _nearest_existing_ancestor(root.parent)
    staging_device = staging_ancestor.stat().st_dev
    requirements: dict[int, dict[str, Any]] = {
        staging_device: {
            "path": staging_ancestor,
            "bytes": total_bytes + margin,
            "reason": "restore staging",
        }
    }
    for row in rows:
        role = str(row.get("role") or "")
        target = targets[role]
        probe = target if target.exists() and target.is_dir() else target.parent
        ancestor = _nearest_existing_ancestor(probe)
        device = ancestor.stat().st_dev
        if device == staging_device:
            continue
        required = int(row.get("regular_file_bytes") or 0)
        bucket = requirements.setdefault(
            device,
            {"path": ancestor, "bytes": margin, "reason": "external restore targets"},
        )
        bucket["bytes"] = int(bucket["bytes"]) + required
    for item in requirements.values():
        free = shutil.disk_usage(Path(item["path"])).free
        required = int(item["bytes"] or 0)
        if free < required:
            raise BackupError(
                f"insufficient free space for {item['reason']} near {item['path']}: "
                f"need at least {required} bytes, have {free}"
            )


def _run_init(root: Path, *, configured_global: Path) -> None:
    script = root / "init-awoki.sh"
    if not script.exists():
        raise BackupError("destination is missing init-awoki.sh")
    env = os.environ.copy()
    env["AWOKI_ROOT"] = str(root)
    env["AWOKI_GLOBAL_ROOT"] = str(configured_global)
    completed = subprocess.run([str(script)], cwd=root, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BackupError(f"restored data but layout initialization failed: {detail}")


def _reindex_restored(
    root: Path,
    *,
    primary_global_root: Path,
    additional_global_roots: list[Path],
    mode: str,
) -> dict[str, Any]:
    if mode == "none":
        return {"status": "skipped", "mode": "none"}
    include_qdrant = mode == "vector"
    env_before = os.environ.copy()
    sys_path_added = str(root / ".harness")
    try:
        os.environ["AWOKI_ROOT"] = str(root)
        os.environ["HARNESS_ROOT"] = str(root)
        os.environ["AWOKI_GLOBAL_ROOT"] = str(primary_global_root)
        os.environ["HARNESS_GLOBAL_ROOT"] = str(primary_global_root)
        sys.path.insert(0, sys_path_added)
        from harness_core import HarnessPaths, index_global, index_project  # type: ignore
        import project_workspace  # type: ignore

        primary_paths = HarnessPaths(root=root, global_root=primary_global_root)
        projects: list[dict[str, Any]] = []
        for row in project_workspace.project_list(root):
            project_id = str(row.get("project_id") or "")
            if project_id:
                projects.append(index_project(
                    include_artifacts=True,
                    include_code=False,
                    include_qdrant=include_qdrant,
                    project_id=project_id,
                    paths=primary_paths,
                ))

        roots: list[Path] = []
        for candidate in [primary_global_root, *additional_global_roots]:
            resolved = candidate.resolve()
            if resolved not in roots:
                roots.append(resolved)
        if include_qdrant and len(roots) > 1:
            # Qdrant uses one global scope in one collection. Indexing each root
            # independently would replace the previous root's vectors, so vector
            # restore indexes only the preferred Docker/SSH global root. Lexical
            # indexes remain independently rebuildable for every restored root.
            vector_warning = (
                "multiple global roots were restored; semantic global vectors were rebuilt only for "
                f"{primary_global_root}. Re-run global indexing with an explicit AWOKI_GLOBAL_ROOT "
                "when using another runtime root."
            )
            roots_to_index = [primary_global_root.resolve()]
        else:
            vector_warning = ""
            roots_to_index = roots

        global_results: list[dict[str, Any]] = []
        for global_root in roots_to_index:
            global_results.append(index_global(
                include_qdrant=include_qdrant,
                paths=HarnessPaths(root=root, global_root=global_root),
            ))
        return {
            "status": "indexed",
            "mode": mode,
            "project_count": len(projects),
            "projects": projects,
            "global": global_results[0] if global_results else {},
            "globals": global_results,
            "global_roots": [str(path) for path in roots_to_index],
            "warning": vector_warning,
        }
    except Exception as exc:
        raise BackupError(
            f"restore completed, but {mode} reindexing failed: {exc}. "
            "The canonical data is restored; rerun `make index` or `make index-vector`."
        ) from exc
    finally:
        os.environ.clear()
        os.environ.update(env_before)
        with contextlib.suppress(ValueError):
            sys.path.remove(sys_path_added)



def _tree_conflicts(
    path: Path,
    *,
    allowed_placeholders: dict[str, str | None] | None = None,
    ignored_top_level: set[str] | None = None,
) -> list[Path]:
    allowed_placeholders = allowed_placeholders or {}
    ignored_top_level = ignored_top_level or set()
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        return [path]
    if path.is_file():
        return _meaningful_entries(path)
    conflicts: list[Path] = []
    for child in path.rglob("*"):
        try:
            relative = child.relative_to(path)
        except ValueError:
            conflicts.append(child)
            continue
        if relative.parts and relative.parts[0] in ignored_top_level:
            continue
        if child.is_dir() and not child.is_symlink():
            continue
        if child.is_symlink():
            conflicts.append(child)
            continue
        if child.stat().st_size == 0:
            continue
        key = relative.as_posix()
        if key in allowed_placeholders:
            expected = allowed_placeholders[key]
            if expected is None:
                continue
            try:
                if child.read_text(encoding="utf-8") == expected:
                    continue
            except (OSError, UnicodeError):
                pass
        conflicts.append(child)
    return conflicts

def restore_backup(
    root: Path,
    archive: Path,
    *,
    force: bool = False,
    stop_containers: bool = False,
    reindex: str = "auto",
) -> dict[str, Any]:
    root = root.resolve()
    if reindex not in {"auto", "none", "lexical", "vector"}:
        raise BackupError(f"unsupported reindex mode: {reindex}")
    verification = verify_backup(archive, require_checksum=True)
    manifest = verification["manifest"]
    mode = str(manifest["mode"])

    with _operation_lock(root):
        running = _running_compose_services(root)
        stopped: list[str] = []
        if running and stop_containers:
            stopped = _stop_compose(root, running)
            running = _running_compose_services(root)
        qdrant_running = any(row.get("service") == "qdrant" for row in running)
        if qdrant_running:
            raise BackupError(
                "Qdrant is running; restore may invalidate or replace its storage and is never allowed live. "
                "Use --stop-containers or stop both Compose projects first."
            )
        if running:
            details = ", ".join(f"{row['compose_file']}:{row['service']}" for row in running)
            raise BackupError(
                "Awoki containers are running; restore is never applied to live runtime data "
                f"({details}). Use --stop-containers or stop both Compose projects first."
            )

        with tarfile.open(Path(archive).expanduser().resolve(), mode="r:gz") as tar:
            archive_dotenv = _archive_dotenv(tar, manifest)
            targets = _restore_targets(root, manifest)
            rows = [row for row in manifest.get("payloads") or [] if isinstance(row, dict)]
            _validate_restore_targets(root, rows, targets)
            if archive_dotenv is not None:
                archived_roots = _configured_roots(
                    root, dotenv_override=archive_dotenv, include_environment=False
                )
                current_roots = _configured_roots(root)
                for key in ("configured_global", "configured_skills"):
                    if archived_roots[key] != current_roots[key]:
                        raise BackupError(
                            "the archived .env resolves a different destination path for "
                            f"{key}: archived={archived_roots[key]}, destination={current_roots[key]}. "
                            "Configure the destination consistently or restore without the archived .env."
                        )
            _check_restore_disk_space(root, rows, targets)
            if mode == "full":
                issues = _full_compatibility_issues(root, manifest)
                if issues and not force:
                    raise BackupError(
                        "full restore compatibility checks failed; use a portable restore/reindex or explicitly --force:\n  "
                        + "\n  ".join(issues)
                    )
            if mode == "portable" and not force:
                _invalidate_portable_indexes(root, targets, force=False)

            conflicts: list[str] = []
            placeholders: dict[str, dict[str, str | None]] = {
                **TRACKED_RUNTIME_READMES,
                "workspace": WORKSPACE_PLACEHOLDERS,
                "global_repo": {"README.md": GLOBAL_README_PLACEHOLDER},
                "global_configured": {"README.md": GLOBAL_README_PLACEHOLDER},
            }
            # Clean installation scaffolding is ignored only at exact known paths.
            # A README inside a restored repository is canonical data and therefore
            # remains a conflict rather than being ignored by basename.
            for row in rows:
                role = str(row.get("role") or "")
                target = targets.get(role)
                if target is None:
                    raise BackupError(f"unknown payload role in archive: {role}")
                if target.is_dir():
                    ignored_top = {"cache", "npm"} if role == "opencode_state" else set()
                    conflicts.extend(str(item) for item in _tree_conflicts(
                        target,
                        allowed_placeholders=placeholders.get(role, {}),
                        ignored_top_level=ignored_top,
                    ))
                elif role == "harness_notes":
                    conflicts.extend(str(item) for item in _meaningful_entries(
                        target, placeholder_text=HARNESS_NOTES_PLACEHOLDER
                    ))
                else:
                    conflicts.extend(str(item) for item in _meaningful_entries(target))
            if conflicts and not force:
                raise BackupError(
                    "restore would overwrite existing Awoki runtime data; use a clean installation or --force:\n  "
                    + "\n  ".join(conflicts[:12])
                )

            with tempfile.TemporaryDirectory(prefix="awoki-restore-", dir=str(root.parent)) as temp_dir:
                staging = Path(temp_dir)
                # Fully extract and validate before deleting any destination data.
                staged = _extract_payload_to_staging(tar, manifest, staging)
                pre_apply_running = _running_compose_services(root)
                if pre_apply_running:
                    details = ", ".join(
                        f"{row['compose_file']}:{row['service']}" for row in pre_apply_running
                    )
                    raise BackupError(
                        "Awoki services started during restore staging; no destination data was changed "
                        f"({details})"
                    )
                if mode == "portable" and force:
                    _invalidate_portable_indexes(root, targets, force=True)
                if force:
                    for row in rows:
                        role = str(row.get("role") or "")
                        target = targets[role]
                        preserve_names: set[str] = set()
                        preserve_relative_paths: set[str] = set()
                        preserve_top_level: set[str] = set()
                        if role == "harness_state":
                            preserve_names.update({"README.md", LOCK_NAME})
                        elif role == "harness_artifacts":
                            preserve_relative_paths.update(TRACKED_RUNTIME_READMES["harness_artifacts"])
                        elif role == "harness_index":
                            preserve_names.add("README.md")
                        elif role == "opencode_state":
                            preserve_top_level.update({"cache", "npm"})
                        _clear_target(
                            target,
                            preserve_names=preserve_names,
                            preserve_relative_paths=preserve_relative_paths,
                            preserve_top_level=preserve_top_level,
                        )
                for row in rows:
                    role = str(row.get("role") or "")
                    staged_path = staged.get(role)
                    if staged_path is None:
                        continue
                    _apply_staged_payload(staged_path, targets[role])

        payload_roles = {str(row.get("role") or "") for row in rows}
        primary_global = targets["global_repo"]
        additional_globals = (
            [targets["global_configured"]]
            if "global_configured" in payload_roles and targets["global_configured"] != primary_global
            else []
        )
        _run_init(root, configured_global=primary_global)
        effective_reindex = ("lexical" if mode == "portable" else "none") if reindex == "auto" else reindex
        try:
            reindex_result = _reindex_restored(
                root,
                primary_global_root=primary_global,
                additional_global_roots=additional_globals,
                mode=effective_reindex,
            )
            restore_status = "restored"
        except BackupError as exc:
            reindex_result = {"status": "failed", "mode": effective_reindex, "error": str(exc)}
            restore_status = "restored_with_reindex_warning"

    return {
        "status": restore_status,
        "archive": str(Path(archive).expanduser().resolve()),
        "mode": mode,
        "sha256": verification["sha256"],
        "force": force,
        "containers_stopped": stopped,
        "payload_roles": [str(row.get("role") or "") for row in manifest.get("payloads") or [] if isinstance(row, dict)],
        "primary_global_root": str(primary_global),
        "additional_global_roots": [str(path) for path in additional_globals],
        "reindex": reindex_result,
        "next_steps": [
            "Run `.harness/bin/awoki doctor`.",
            "For semantic vectors after a portable restore, run `make index-vector` when Qdrant and the embedding endpoint are available.",
            "Rebuild/start the OpenCode SSH image with `make install-opencode-ssh` or `./run-opencode.sh`.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Awoki runtime backup and restore")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a portable or full runtime backup")
    create.add_argument("--mode", choices=("portable", "full"), required=True)
    create.add_argument("--output-dir", type=Path, default=Path("../awoki-backups"))
    create.add_argument("--include-opencode-state", action="store_true", help="include OpenCode session/config state; may contain credentials")
    create.add_argument("--include-secrets", action="store_true", help="include .env and SSH client material")
    create.add_argument("--allow-live", action="store_true", help="explicitly accept a potentially inconsistent live backup")
    create.add_argument("--stop-containers", action="store_true", help="stop running Awoki Compose projects before capture; they remain stopped")

    verify = sub.add_parser("verify", help="verify archive checksum, manifest, and safe member paths")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--allow-missing-checksum", action="store_true")

    inspect = sub.add_parser("inspect", help="show a verified archive manifest without restoring")
    inspect.add_argument("archive", type=Path)

    restore = sub.add_parser("restore", help="restore a verified runtime backup into this installation")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--force", action="store_true", help="clear conflicting managed runtime data and accept full-backup compatibility differences")
    restore.add_argument("--stop-containers", action="store_true", help="stop running Awoki Compose projects before restore; they remain stopped")
    restore.add_argument("--reindex", choices=("auto", "none", "lexical", "vector"), default="auto")

    sub.add_parser("lock-check", help="refuse an active maintenance operation and remove a stale PID lock")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(os.environ.get("AWOKI_ROOT", Path(__file__).resolve().parents[1])).resolve()
    try:
        if args.command == "create":
            result = create_backup(
                root,
                mode=args.mode,
                output_dir=args.output_dir,
                include_opencode_state=args.include_opencode_state,
                include_secrets=args.include_secrets,
                allow_live=args.allow_live,
                stop_containers=args.stop_containers,
            )
        elif args.command == "verify":
            result = verify_backup(args.archive, require_checksum=not args.allow_missing_checksum)
            result.pop("manifest", None)
        elif args.command == "inspect":
            result = inspect_backup(args.archive)
        elif args.command == "restore":
            result = restore_backup(
                root,
                args.archive,
                force=args.force,
                stop_containers=args.stop_containers,
                reindex=args.reindex,
            )
        elif args.command == "lock-check":
            result = _check_operation_lock(root, recover_stale=True)
        else:
            return 2
    except BackupError as exc:
        print(f"[awoki-backup] ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
