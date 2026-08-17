from __future__ import annotations

import hashlib
import os
import json
import re
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

INDEX_POLICY_VERSION = 4

SAFE_TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".jsonl", ".c", ".h", ".cpp", ".hpp",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".asm", ".s", ".log", ".yara", ".yar", ".yaml", ".yml", ".toml",
    ".sql", ".sh",
}

# Additional text formats accepted only for repository-code indexing. Keeping
# this separate avoids broadening general artifact/RAG eligibility merely
# because a language is useful to the structural code engine.
CODE_SAFE_SUFFIXES = {
    ".pyi", ".mjs", ".cjs", ".cc", ".cxx", ".hh", ".hxx", ".cs",
    ".bash", ".rb", ".php", ".vue", ".svelte", ".kt", ".kts", ".scala",
    ".lua", ".r", ".swift", ".dart", ".ex", ".exs", ".erl", ".hrl",
    ".sol", ".tf", ".hcl", ".xml", ".gradle", ".properties",
}

CODE_SAFE_NAMES = {
    "dockerfile", "makefile", "rakefile", "gemfile", "jenkinsfile",
}

NEVER_INDEX_SUFFIXES = {
    ".env", ".har", ".http", ".pem", ".key", ".p12", ".pfx", ".der",
    ".crt", ".cer", ".jks", ".keystore", ".sqlite", ".sqlite3", ".db",
}

NEVER_INDEX_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target",
    "dist", "build", "raw", "private_keys",
}

# Repository source must not disappear merely because a package/directory is
# named `credentials`, `secrets`, etc. Those names are common in authentication
# and security code. For code indexing, only generated/cache directories are
# blanket-excluded. Sensitive data/config files are still blocked by explicit
# filename/suffix rules below.
CODE_NEVER_INDEX_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "target",
    "dist", "build", ".mypy_cache", ".ruff_cache", ".tox", "coverage", ".next",
}

# Analysis/report paths are semantic evidence. Names such as `build`, `dist`,
# `credentials`, or `secrets` must not censor a report merely because they are
# meaningful in the subject being analyzed. Raw/private-key containers remain
# excluded from broad semantic retrieval.
ANALYSIS_NEVER_INDEX_PARTS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "node_modules",
    "raw", "private_keys",
}

ANALYSIS_CATEGORIES = {"artifact", "project_view", "analysis", "report"}

NEVER_INDEX_NAMES = {
    ".npmrc", ".pypirc", ".netrc", "credentials.json", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
}

NO_RAG_MARKERS = ("awoki:no-rag", "awoki: no-rag", "no-rag: true", "index_policy: no_rag")


def source_role(path: str | Path) -> str:
    """Classify repository paths for ranking/evidence without excluding tests.

    Tests and fixtures remain first-class searchable evidence. The role is a
    presentation/ranking hint only; production source stays authoritative for
    claims about runtime implementation unless the user explicitly asks about
    tests.
    """
    raw = str(path).replace("\\", "/").strip("/")
    parts = [part.lower() for part in raw.split("/") if part]
    name = parts[-1] if parts else ""
    suffix = Path(name).suffix.lower()
    lower_raw = "/".join(parts)
    if any(part in {"testdata", "fixtures", "fixture", "test-fixtures", "test_fixtures"} for part in parts):
        return "test_fixture"
    if (
        any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
        or name.endswith("_test.go")
        or re_search_test_name(name)
    ):
        return "test"
    if any(part in {"vendor", "node_modules", "dist", "build", "target"} for part in parts[:-1]):
        return "generated_or_vendor"
    if any(part in {"generated", "gen", "stub", "stubs"} for part in parts[:-1]):
        return "generated_or_vendor"
    if (
        name.endswith(".schema.json")
        or any(part in {".schema", ".schemas", "schema", "schemas"} for part in parts[:-1])
        or (suffix in {".json", ".yaml", ".yml", ".toml"} and any(part in {"config", "configs"} for part in parts[:-1]))
    ):
        return "config_schema"
    if (
        suffix in {".md", ".markdown", ".rst", ".adoc"}
        or any(part in {"doc", "docs", "documentation"} for part in parts[:-1])
        or lower_raw.startswith(".github/issue_template/")
        or name.endswith("_doc.go")
    ):
        return "documentation"
    return "production"


def re_search_test_name(name: str) -> bool:
    return bool(re.search(r"(?:^|[._-])(test|tests|spec|specs)(?:[._-]|$)", name, flags=re.IGNORECASE))


@dataclass(frozen=True)
class FileDecision:
    path: str
    included: bool
    reason: str
    content_hash: str = ""
    size_bytes: int = 0
    category: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def looks_textual_bytes(data: bytes) -> bool:
    """Conservative textual-file detector for repository lexical coverage.

    Parser support must not define the exhaustive search universe. Unknown
    textual formats remain eligible for exhaustive lexical search; curated
    source/interface/policy formats may additionally use the structural engine's
    deterministic text-fallback parser. NUL-heavy/binary blobs stay excluded.
    """
    sample = data[:65536]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        decoded = sample.decode("utf-8", errors="replace")
        if not decoded:
            return False
        replacement_ratio = decoded.count("\ufffd") / max(1, len(decoded))
        control = sum(1 for ch in decoded if ord(ch) < 32 and ch not in "\n\r\t\f\b")
        return replacement_ratio < 0.02 and control / max(1, len(decoded)) < 0.01


def read_safe_artifact_registry(index_dir: Path) -> set[str]:
    registry = index_dir / "safe_artifacts.jsonl"
    out: set[str] = set()
    if not registry.exists():
        return out
    for line in registry.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("path") and row.get("status", "active") == "active":
            out.add(str(row["path"]).strip().replace("\\", "/"))
    return out


def _has_no_rag_marker(path: Path, data: bytes) -> bool:
    if path.with_name(path.name + ".no-rag").exists() or path.with_suffix(path.suffix + ".no-rag").exists():
        return True
    head = data[:8192].decode("utf-8", errors="ignore").lower()
    return any(marker in head for marker in NO_RAG_MARKERS)


def _looks_like_safe_summary(rel: Path) -> bool:
    name = rel.name.lower()
    return rel.suffix.lower() in {".md", ".txt", ".json"} and any(
        token in name for token in ("summary", "report", "finding", "observation", "handoff", "latest")
    )




def is_explicit_sensitive_path(path: Path) -> bool:
    """Return whether a path is an obvious credential/raw-secret data file.

    Code analysis remains coverage-first: these textual files may participate in
    local exhaustive lexical search, but they are never structurally indexed or
    embedded and their match/context previews are opaque.
    """
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    return bool(
        lower_name == ".env"
        or lower_name.startswith(".env.")
        or lower_name in NEVER_INDEX_NAMES
        or suffix in NEVER_INDEX_SUFFIXES
    )


def source_evidence_path_allowed(path: Path, *, repo_root: Path | None = None, max_bytes: int = 2_000_000) -> tuple[bool, str]:
    """Lightweight policy gate for exact source-evidence identifiers.

    Evidence ids are self-contained checksummed references, not authenticated
    capabilities. A caller can therefore fabricate one. Verification must not
    turn that into an oracle for files that ``code_source_window`` itself would
    never expose. This gate mirrors the path/size/no-RAG boundaries of normal
    structural source evidence without applying semantic secret vocabulary to
    ordinary source code.
    """
    if path.is_symlink():
        return False, "symlink_not_allowed"
    try:
        stat = path.stat()
    except OSError as exc:
        return False, f"stat_failed:{exc}"
    if not path.is_file():
        return False, "not_a_file"
    if is_explicit_sensitive_path(path):
        return False, "explicit_sensitive_path"
    prose_suffixes = {".md", ".markdown", ".rst", ".adoc", ".txt", ".log", ".csv", ".tsv"}
    prose_names = {"readme", "license", "notice", "changelog", "authors", "contributors"}
    lower_name = path.name.lower()
    stem = path.stem.lower() if path.suffix else lower_name
    if path.suffix.lower() in prose_suffixes or lower_name in prose_names or stem in prose_names:
        return False, "prose_lexical_only"
    try:
        rel = path.relative_to(repo_root) if repo_root is not None else path
        rel_parts = {part.lower() for part in rel.parts}
    except (ValueError, OSError):
        rel_parts = {part.lower() for part in path.parts}
    blocked = sorted(rel_parts & CODE_NEVER_INDEX_PARTS)
    if blocked:
        return False, f"lexical_only_path_component:{blocked[0]}"
    if stat.st_size > max_bytes:
        return False, "large_text_lexical_only"
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError as exc:
        return False, f"read_failed:{exc}"
    if _has_no_rag_marker(path, head):
        return False, "no_rag_marker"
    if not looks_textual_bytes(head):
        return False, "nontext_not_source_evidence"
    return True, "source_evidence_allowed"

def decide_file(
    path: Path,
    *,
    rel: Path,
    category: str,
    redact: Callable[[str], tuple[str, bool]],
    registered_safe: Iterable[str] = (),
    max_bytes: int = 2_000_000,
    strict_artifacts: bool = False,
) -> FileDecision:
    rel_text = rel.as_posix()
    if path.is_symlink():
        return FileDecision(rel_text, False, "symlink_not_allowed", category=category)
    try:
        stat = path.stat()
    except OSError as exc:
        return FileDecision(rel_text, False, f"stat_failed:{exc}", category=category)
    if not path.is_file():
        return FileDecision(rel_text, False, "not_a_file", size_bytes=stat.st_size, category=category)
    lowered_parts = {part.lower() for part in rel.parts}
    if category == "code":
        blocked_parts = CODE_NEVER_INDEX_PARTS
    elif category in ANALYSIS_CATEGORIES:
        blocked_parts = ANALYSIS_NEVER_INDEX_PARTS
    else:
        blocked_parts = NEVER_INDEX_PARTS
    blocked = sorted(lowered_parts & blocked_parts)
    code_path_lexical_only = ""
    if blocked:
        if category != "code" or blocked[0] == ".git":
            return FileDecision(rel_text, False, f"excluded_path_component:{blocked[0]}", size_bytes=stat.st_size, category=category)
        # Coverage-first repository search: generated/vendor/cache path names are
        # not a reason to make tracked textual code disappear. Keep those files
        # out of structural/vector indexing, but allow local exhaustive lexical
        # search and record the path policy explicitly.
        code_path_lexical_only = blocked[0]
    suffix = path.suffix.lower()
    lower_name = path.name.lower()
    sensitive_path = is_explicit_sensitive_path(path)
    analysis_sensitive_text = bool(sensitive_path and category in ANALYSIS_CATEGORIES)
    if sensitive_path and category != "code" and category not in ANALYSIS_CATEGORIES:
        return FileDecision(rel_text, False, f"excluded_sensitive_extension:{suffix or path.name}", size_bytes=stat.st_size, category=category)
    category_allows_code_text = category == "code" and (suffix in CODE_SAFE_SUFFIXES or lower_name in CODE_SAFE_NAMES)
    if category != "code" and suffix not in SAFE_TEXT_SUFFIXES and not analysis_sensitive_text:
        return FileDecision(rel_text, False, f"unsupported_extension:{suffix or '<none>'}", size_bytes=stat.st_size, category=category)
    if stat.st_size > max_bytes and category != "code":
        return FileDecision(rel_text, False, "file_too_large", size_bytes=stat.st_size, category=category)
    if strict_artifacts:
        registry = {str(v).strip().replace("\\", "/") for v in registered_safe}
        project_rel = rel_text.split("/artifacts/", 1)[-1] if "/artifacts/" in rel_text else rel_text
        if rel_text not in registry and project_rel not in registry and not _looks_like_safe_summary(rel):
            return FileDecision(rel_text, False, "artifact_not_registered_or_safe_summary", size_bytes=stat.st_size, category=category)
    if stat.st_size > max_bytes and category == "code":
        try:
            with path.open("rb") as handle:
                sample = handle.read(65536)
        except OSError as exc:
            return FileDecision(rel_text, False, f"read_failed:{exc}", size_bytes=stat.st_size, category=category)
        if _has_no_rag_marker(path, sample):
            return FileDecision(rel_text, False, "no_rag_marker", "", stat.st_size, category)
        if not looks_textual_bytes(sample):
            return FileDecision(rel_text, False, "file_too_large_nontext", "", stat.st_size, category)
        try:
            digest = content_hash_file(path)
        except OSError as exc:
            return FileDecision(rel_text, False, f"hash_failed:{exc}", size_bytes=stat.st_size, category=category)
        if sensitive_path:
            reason = "sensitive_text_lexical_only"
        elif code_path_lexical_only:
            reason = "generated_text_lexical_only"
        else:
            reason = "large_text_lexical_only"
        return FileDecision(rel_text, True, reason, digest, stat.st_size, category)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return FileDecision(rel_text, False, f"read_failed:{exc}", size_bytes=stat.st_size, category=category)
    digest = content_hash_bytes(data)
    if _has_no_rag_marker(path, data):
        return FileDecision(rel_text, False, "no_rag_marker", digest, stat.st_size, category)
    text = data.decode("utf-8", errors="ignore")
    if category != "code":
        _, sensitive = redact(text)
        # Reports, generated project views, and registered analysis artifacts are
        # coverage-first. A credential value may be redacted when stored, but a
        # security finding must not disappear merely because it discusses auth.
        if category in {"artifact", "project_view", "analysis", "report"}:
            return FileDecision(
                rel_text, True,
                "sanitized_analysis_allowlist" if sensitive else "safe_allowlist",
                digest, stat.st_size, category,
            )
        if sensitive:
            return FileDecision(rel_text, False, "sensitive_content_detected", digest, stat.st_size, category)
        return FileDecision(rel_text, True, "safe_allowlist", digest, stat.st_size, category)

    # Exhaustive repository search is based on textual repository content, not
    # on the curated parser-extension list. Unknown textual languages/configs
    # remain lexically searchable even when they are not admitted to the
    # structural parser/text-fallback set. Explicit secret files and
    # generated/vendor path names are lexical-only rather than blind spots.
    # Explicit no-rag markers remain the intentional user-controlled exclusion;
    # binary data remains outside the textual source universe.
    if not (category_allows_code_text or suffix in SAFE_TEXT_SUFFIXES or looks_textual_bytes(data)):
        return FileDecision(rel_text, False, "unsupported_binary_or_nontext", digest, stat.st_size, category)
    if sensitive_path:
        return FileDecision(rel_text, True, "sensitive_text_lexical_only", digest, stat.st_size, category)
    if code_path_lexical_only:
        return FileDecision(rel_text, True, "generated_text_lexical_only", digest, stat.st_size, category)
    reason = "source_code_allowlist" if (category_allows_code_text or suffix in SAFE_TEXT_SUFFIXES) else "source_text_fallback_allowlist"
    return FileDecision(rel_text, True, reason, digest, stat.st_size, category)


def write_index_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_index_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def register_safe_artifact(index_dir: Path, path: str, *, reason: str = "generated_safe_summary", source: str = "awoki") -> dict[str, Any]:
    """Register one sanitized project-relative artifact under an exclusive lock."""
    raw = str(path or "").strip().replace("\\", "/")
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("safe artifact path must be a normalized project-relative path")
    clean = candidate.as_posix()
    if not clean.startswith("artifacts/") or "/raw/" in f"/{clean}/":
        raise ValueError("safe artifact path must be inside artifacts/ and outside raw directories")
    registry = index_dir / "safe_artifacts.jsonl"
    registry.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry.with_suffix(registry.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing = read_safe_artifact_registry(index_dir)
            if clean in existing:
                return {"status": "already_registered", "path": clean}
            row = {"path": clean, "status": "active", "reason": reason, "source": source}
            with registry.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return {"status": "registered", **row}
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
