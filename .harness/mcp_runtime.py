from __future__ import annotations

import importlib.metadata

SUPPORTED_REQUIREMENT = "mcp>=1.29,<2"


class MCPRuntimeError(RuntimeError):
    pass


def validate_mcp_version(version: str) -> str:
    value = str(version).strip()
    try:
        major = int(value.split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise MCPRuntimeError(f"cannot parse installed MCP SDK version: {value or '<empty>'}") from exc
    if major != 1:
        raise MCPRuntimeError(
            "Awoki currently requires MCP Python SDK 1.x; "
            f"found {value}. Rebuild with {SUPPORTED_REQUIREMENT}."
        )
    return value


def installed_mcp_version() -> str:
    try:
        value = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError as exc:
        raise MCPRuntimeError(
            "Awoki MCP dependency is missing; rebuild the image from requirements.txt"
        ) from exc
    return validate_mcp_version(value)
