from __future__ import annotations

import os
import re
from collections.abc import Mapping

# Environment variables matching these names/patterns are never required by
# repository-inspection subprocesses.  Strip them before invoking Git, rg, or
# other tools that operate on potentially untrusted repository state.
_EXPLICIT_SECRET_NAMES = {
    "AWOKI_EMBEDDING_API_KEY",
    "AWOKI_OPENAI_API_KEY",
    "AWOKI_RERANK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "SSH_AUTH_SOCK",
}
_SECRET_NAME_RE = re.compile(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:$|_)", re.IGNORECASE)
_SECRET_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_APPLICATION_CREDENTIALS")
_RUNTIME_SERVICE_PREFIXES = ("AWOKI_EMBEDDING_", "AWOKI_RERANK_", "AWOKI_QDRANT_", "AWOKI_BURP_", "OPENAI_")
_RUNTIME_SERVICE_NAMES = {"QDRANT_URL", "QDRANT_COLLECTION_PROJECT", "QDRANT_COLLECTION_GLOBAL"}

# Variables that can cause Git or a shell/tool helper to execute caller-selected
# programs.  Repository-specific Git config is handled separately by the Git
# callers; these ambient overrides must not leak across the MCP boundary.
_EXECUTION_OVERRIDE_NAMES = {
    "BASH_ENV",
    "ENV",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_PROXY_COMMAND",
    "PAGER",
}
_EXECUTION_OVERRIDE_PREFIXES = ("LD_", "DYLD_", "PYTHON")


def credential_free_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment suitable for repository-facing subprocesses.

    This is defense in depth, not a sandbox: the current stdio MCP and target
    repository still share a Unix uid in the OpenCode SSH container.  The goal
    is to prevent accidental retrieval/service configuration inheritance and
    environment-selected loaders/helpers when deterministic harness tools
    inspect untrusted source.
    """
    env = dict(os.environ if source is None else source)

    indirect_name = env.get("AWOKI_RERANK_API_KEY_ENV", "").strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", indirect_name):
        env.pop(indirect_name, None)

    for name in list(env):
        upper = name.upper()
        if (
            name in _EXPLICIT_SECRET_NAMES
            or _SECRET_NAME_RE.search(name)
            or any(upper.startswith(prefix) for prefix in _SECRET_PREFIXES)
            or any(upper.startswith(prefix) for prefix in _RUNTIME_SERVICE_PREFIXES)
            or name in _RUNTIME_SERVICE_NAMES
            or name in _EXECUTION_OVERRIDE_NAMES
            or any(upper.startswith(prefix) for prefix in _EXECUTION_OVERRIDE_PREFIXES)
        ):
            env.pop(name, None)

    # Keep the indirection *name* only if callers need to describe configuration;
    # repository-facing subprocesses never need to resolve it.
    env.pop("AWOKI_RERANK_API_KEY_ENV", None)
    return env
