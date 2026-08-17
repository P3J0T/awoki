from __future__ import annotations

import re
from typing import Any, Mapping

ALLOWED_REFERENCE_PREFIXES = (
    "secret://", "op://", "pass://", "vault://", "env:",
    "age://", "keyring://", "file://",
)

SECRET_VALUE_KEYS = {
    "password", "passwd", "pwd", "secret", "secret_value", "secret_values",
    "token", "access_token", "refresh_token", "id_token", "api_key",
    "apikey", "private_key", "client_secret", "access_key", "access_key_id",
    "authorization", "cookie", "set_cookie", "session_cookie",
}

# Strict redaction is retained for raw/captured/configuration trust domains. It
# must not be used to decide whether code, security-analysis reports, or Burp
# summaries are allowed to exist in retrieval.
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?im)^Cookie:\s*.*$"),
    re.compile(r"(?im)^Set-Cookie:\s*.*$"),
)


def _replacement(match: re.Match[str]) -> str:
    value = match.group(0)
    lower = value.lower()
    if "-----begin" in lower:
        return "<REDACTED_PRIVATE_KEY>"
    if lower.startswith("authorization"):
        return re.split(r"[:=]", value, maxsplit=1)[0] + ": <REDACTED>"
    if lower.startswith("cookie:"):
        return "Cookie: <REDACTED>"
    if lower.startswith("set-cookie:"):
        return "Set-Cookie: <REDACTED>"
    if lower.startswith("bearer "):
        return "Bearer <REDACTED>"
    if re.match(r"(?i)AKIA", value):
        return "<REDACTED_AWS_ACCESS_KEY_ID>"
    if value.startswith("eyJ"):
        return "<REDACTED_JWT>"
    if value.startswith("sk-"):
        return "<REDACTED_API_KEY>"
    if re.search(r"[:=]", value):
        return re.split(r"[:=]", value, maxsplit=1)[0] + "=<REDACTED>"
    return "<REDACTED>"


def redact_text(value: Any) -> tuple[str, bool]:
    """Strict redaction for raw/captured/configuration material.

    This function is intentionally conservative and may treat assignments to
    secret-like names as sensitive. Do not use it to gate code or analysis
    coverage; use :func:`redact_source_text` / :func:`redact_analysis_text`.
    """
    text = str(value or "")
    redacted = text
    changed = False
    for pattern in SENSITIVE_PATTERNS:
        updated, count = pattern.subn(_replacement, redacted)
        if count:
            changed = True
            redacted = updated
    return redacted, changed


# Code and security-analysis text are coverage-first trust domains. Identifiers
# such as `token`, `password`, `auth`, `secret`, OAuth/JWT vocabulary, endpoint
# names, and expressions are evidence. Only high-confidence values are masked.
SOURCE_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    SENSITIVE_PATTERNS[0],  # PEM/private-key blocks
    SENSITIVE_PATTERNS[1],  # Authorization: Bearer/Basic <credential>
    SENSITIVE_PATTERNS[2],  # standalone long Bearer credential
    SENSITIVE_PATTERNS[4],  # AWS access key id
    SENSITIVE_PATTERNS[5],  # JWT
    SENSITIVE_PATTERNS[6],  # OpenAI-style sk- key
    SENSITIVE_PATTERNS[7],  # Cookie header
    SENSITIVE_PATTERNS[8],  # Set-Cookie header
)

# Match a quoted literal assigned to a credential-like identifier. Requiring a
# quoted RHS avoids classifying ordinary expressions such as
# `token := parseToken(r)` or `password = config.password` as secrets.
_SOURCE_SECRET_LITERAL_RE = re.compile(
    r'''(?ix)
    (?P<prefix>
        (?:["']?)
        (?:password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|credential|credentials|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)
        (?:["']?)
        \s*(?::=|=|:)\s*
    )
    (?P<quote>["'])
    (?P<value>(?:\\.|(?!\2).)*)
    (?P=quote)
    ''',
)

# High-confidence credentials embedded in common connection URLs. Preserve the
# scheme/user/host so the architecture remains analyzable; redact only password.
_CREDENTIAL_URI_RE = re.compile(
    r"(?i)\b(?P<scheme>postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqp|amqps|http|https)://(?P<user>[^\s:/@]+):(?P<password>[^\s/@]+)@"
)

# Common environment-secret sinks with a quoted secret-like key and quoted
# literal value. This catches e.g. os.Setenv("TOKEN", "...") without hiding
# the call, key name, or surrounding security logic.
_ENV_SECRET_CALL_RE = re.compile(
    r'''(?ix)
    (?P<prefix>\b(?:os\.)?(?:setenv|putenv)\s*\(\s*["']
        (?:password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|credential|credentials|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)
        ["']\s*,\s*)
    (?P<quote>["'])(?P<value>(?:\\.|(?!\2).)*)(?P=quote)
    ''',
)


def _source_literal_replacement(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{match.group('quote')}<REDACTED_SECRET>{match.group('quote')}"


def _credential_uri_replacement(match: re.Match[str]) -> str:
    return f"{match.group('scheme')}://{match.group('user')}:<REDACTED_SECRET>@"


def _env_secret_replacement(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{match.group('quote')}<REDACTED_SECRET>{match.group('quote')}"


def redact_source_text(value: Any) -> tuple[str, bool]:
    """Redact obvious credential values while preserving source semantics.

    Coverage wins over speculative secret detection: secret-like identifiers,
    endpoint names, config fields, and non-literal expressions remain intact.
    """
    text = str(value or "")
    redacted = text
    changed = False
    for pattern in SOURCE_SENSITIVE_PATTERNS:
        updated, count = pattern.subn(_replacement, redacted)
        if count:
            changed = True
            redacted = updated
    for pattern, replacement in (
        # Preserve connection topology when a URI contains credentials before
        # applying generic quoted-secret assignment masking.
        (_CREDENTIAL_URI_RE, _credential_uri_replacement),
        (_SOURCE_SECRET_LITERAL_RE, _source_literal_replacement),
        (_ENV_SECRET_CALL_RE, _env_secret_replacement),
    ):
        updated, count = pattern.subn(replacement, redacted)
        if count:
            changed = True
            redacted = updated
    return redacted, changed



_ANALYSIS_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|credential|credentials|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret)\s*(?P<sep>:(?!=)|=(?!=))\s*(?P<value>[A-Za-z0-9_+/=-]{6,})"
)


def _analysis_assignment_replacement(match: re.Match[str]) -> str:
    return f"{match.group('key')}{match.group('sep')}<REDACTED_SECRET>"

def redact_analysis_text(value: Any) -> tuple[str, bool]:
    """Coverage-first redaction for reports, continuity, findings, and Burp summaries.

    Security analysis is expected to contain words and snippets about tokens,
    authentication, cookies, passwords, and secrets. Treat that material as
    evidence, not as a reason to hide the record. Mask only high-confidence
    values using the same value-level policy as source code.
    """
    redacted, changed = redact_source_text(value)
    updated, count = _ANALYSIS_SECRET_ASSIGNMENT_RE.subn(_analysis_assignment_replacement, redacted)
    return updated, bool(changed or count)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")


def _allowed_reference(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith(ALLOWED_REFERENCE_PREFIXES)


def redact_nested(value: Any) -> tuple[Any, bool]:
    """Strict nested redaction for raw/configuration trust domains."""
    if isinstance(value, str):
        if _allowed_reference(value):
            return value, False
        return redact_text(value)
    if isinstance(value, list):
        out: list[Any] = []
        changed = False
        for item in value:
            safe, item_changed = redact_nested(item)
            out.append(safe)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, tuple):
        safe, changed = redact_nested(list(value))
        return safe, changed
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            key_text = str(key)
            normalized = _normalized_key(key)
            if normalized in SECRET_VALUE_KEYS and not (
                normalized in {"secret", "secret_value"} and _allowed_reference(item)
            ):
                out[key_text] = "<REDACTED>"
                changed = changed or item not in (None, "", [], {})
                continue
            safe, item_changed = redact_nested(item)
            out[key_text] = safe
            changed = changed or item_changed
        return out, changed
    return value, False


def _redact_nested_with(value: Any, redactor) -> tuple[Any, bool]:
    if isinstance(value, str):
        if _allowed_reference(value):
            return value, False
        return redactor(value)
    if isinstance(value, list):
        out: list[Any] = []
        changed = False
        for item in value:
            safe, item_changed = _redact_nested_with(item, redactor)
            out.append(safe)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, tuple):
        safe, changed = _redact_nested_with(list(value), redactor)
        return safe, changed
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            safe, item_changed = _redact_nested_with(item, redactor)
            out[str(key)] = safe
            changed = changed or item_changed
        return out, changed
    return value, False


def redact_source_nested(value: Any) -> tuple[Any, bool]:
    """Recursively sanitize code-tool output without key-name censorship."""
    return _redact_nested_with(value, redact_source_text)


def redact_analysis_nested(value: Any) -> tuple[Any, bool]:
    """Recursively sanitize analysis records without hiding security semantics.

    Mapping keys remain visible because names such as ``access_token`` or
    ``authorization`` are often the evidence being analyzed. When such a key
    directly contains a scalar credential value, redact only the value.
    """
    if isinstance(value, str):
        if _allowed_reference(value):
            return value, False
        return redact_analysis_text(value)
    if isinstance(value, list):
        out: list[Any] = []
        changed = False
        for item in value:
            safe, item_changed = redact_analysis_nested(item)
            out.append(safe)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, tuple):
        safe, changed = redact_analysis_nested(list(value))
        return safe, changed
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            key_text = str(key)
            normalized = _normalized_key(key)
            if normalized in SECRET_VALUE_KEYS and not _allowed_reference(item) and isinstance(item, (str, int, float, bool)):
                out[key_text] = "<REDACTED_SECRET>"
                changed = changed or item not in (None, "")
                continue
            safe, item_changed = redact_analysis_nested(item)
            out[key_text] = safe
            changed = changed or item_changed
        return out, changed
    return value, False
