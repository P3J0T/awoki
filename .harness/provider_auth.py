from __future__ import annotations

from collections.abc import Mapping
import os
import re


HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def provider_auth_settings(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    env = os.environ if environment is None else environment
    mode = (env.get("AWOKI_AI_AUTH_MODE") or "bearer").strip().lower()
    if mode not in {"bearer", "header"}:
        raise ValueError("AWOKI_AI_AUTH_MODE must be 'bearer' or 'header'")
    header = (env.get("AWOKI_AI_AUTH_HEADER") or "Authorization").strip()
    if mode == "bearer" or header.lower() == "authorization":
        header = "Authorization"
    if not HEADER_NAME_RE.fullmatch(header):
        raise ValueError("AWOKI_AI_AUTH_HEADER must be a valid HTTP header name")
    if header.lower() in {"content-type", "user-agent"}:
        raise ValueError("AWOKI_AI_AUTH_HEADER cannot be Content-Type or User-Agent")
    return mode, header


def provider_auth_headers(
    api_key: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if not api_key:
        return {}
    mode, header = provider_auth_settings(environment)
    value = f"Bearer {api_key}" if mode == "bearer" else api_key
    return {header: value}


def provider_error_summary(exc: Exception) -> str:
    """Never forward provider response bodies, URLs, or exception messages.

    Gateways sometimes echo request credentials in errors. Redacting recognized
    token formats alone cannot make an arbitrary provider body safe for the LLM.
    """
    kind = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:80] or "ProviderError"
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        return f"{kind} (HTTP {status}); provider details withheld"
    return f"{kind}; provider details withheld"
