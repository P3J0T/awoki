"""The JSONC dialect used by both project and user OpenCode configuration."""
from __future__ import annotations

import json
from typing import Any


def strip_jsonc(text: str) -> str:
    # Preserve whitespace/newlines for useful JSON decoder locations. Comments
    # separate tokens; deleting them must never turn `1/*...*/2` into `12`.
    out = list(text)
    i = 0
    quoted = escaped = False
    while i < len(text):
        c = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif text[i:i + 2] in ("//", "/*"):
            start = i
            if text[i:i + 2] == "//":
                while i < len(text) and text[i] not in "\r\n":
                    i += 1
            else:
                end = text.find("*/", i + 2)
                if end < 0:
                    raise ValueError("unterminated JSONC block comment")
                i = end + 2
            for j in range(start, i):
                if out[j] not in "\r\n":
                    out[j] = " "
            continue
        i += 1
    stripped = "".join(out)
    quoted = escaped = False
    for i, c in enumerate(stripped):
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c == ",":
            j = i + 1
            while j < len(stripped) and stripped[j].isspace():
                j += 1
            if j < len(stripped) and stripped[j] in "}]":
                out[i] = " "
    return "".join(out)


def loads(text: str) -> Any:
    return json.loads(strip_jsonc(text))


def validate_provider_config(config: Any) -> None:
    """Offline checks for the provider fields Awoki edits; not a full SDK schema."""
    if not isinstance(config, dict):
        raise ValueError("OpenCode configuration must be an object")
    providers = config.get("provider", {})
    if not isinstance(providers, dict):
        raise ValueError("OpenCode provider configuration must be an object")
    for provider in providers.values():
        if not isinstance(provider, dict):
            raise ValueError("each provider must be an object")
        options = provider.get("options", {})
        models = provider.get("models", {})
        if not isinstance(options, dict) or not isinstance(models, dict):
            raise ValueError("provider options and models must be objects")
        headers = options.get("headers", {})
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise ValueError("provider headers must be an object of string values")
        if len({name.lower() for name in headers}) != len(headers):
            raise ValueError("provider headers contain case-insensitive duplicates")
        for model in models.values():
            if not isinstance(model, dict):
                raise ValueError("each model must be an object")
            limits = model.get("limit", {})
            if not isinstance(limits, dict):
                raise ValueError("model limit must be an object")
            for key in ("context", "output"):
                if key in limits and (type(limits[key]) is not int or limits[key] <= 0):
                    raise ValueError("model context/output limits must be positive integers")
            if "context" in limits and "output" in limits and limits["output"] > limits["context"]:
                raise ValueError("model output limit must not exceed context limit")
