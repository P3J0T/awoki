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
    if mode == "bearer":
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
