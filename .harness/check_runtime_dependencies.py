#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".harness" / "runtime-dependencies.lock.json"


def fail(message: str) -> None:
    print(f"[awoki] runtime dependency contract failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    critical = lock["critical_runtime"]
    opencode = critical["opencode_cli"]
    plugin = critical["opencode_plugin_api"]
    sdk = critical["opencode_sdk_api"]
    if str(opencode.get("default_channel") or "") != "latest":
        fail("OpenCode default channel must remain latest; safe mode is the explicit rollback path")
    if str(plugin.get("install_policy") or "") != "match-resolved-opencode-cli":
        fail("OpenCode plugin must match the resolved CLI version")
    if str(sdk.get("install_policy") or "") != "match-resolved-opencode-cli":
        fail("OpenCode SDK must match the resolved CLI version")

    dockerfile = (ROOT / "Dockerfile.opencode").read_text(encoding="utf-8")
    for marker in (
        "ARG OPENCODE_INSTALL_MODE=latest",
        "ARG OPENCODE_SAFE_VERSION=",
        'latest) opencode_spec="opencode-ai@latest"',
        'opencode_spec="opencode-ai@${OPENCODE_SAFE_VERSION}"',
        'npm install --prefix /awoki/.opencode --package-lock=false --save-exact',
        '/awoki/.harness/bin/opencode-runtime-compat-check --materialize',
        'channel_state="latest_untested"',
        "OPENCODE_DISABLE_AUTOUPDATE=1",
    ):
        if marker not in dockerfile:
            fail(f"Dockerfile.opencode missing dynamic OpenCode compatibility marker: {marker}")

    compose_opencode = (ROOT / "docker-compose.opencode.yml").read_text(encoding="utf-8")
    for marker in (
        "OPENCODE_INSTALL_MODE: ${AWOKI_OPENCODE_INSTALL_MODE:-latest}",
        "OPENCODE_SAFE_VERSION: ${AWOKI_OPENCODE_SAFE_VERSION:-}",
        'OPENCODE_DISABLE_AUTOUPDATE: "1"',
    ):
        if marker not in compose_opencode:
            fail(f"docker-compose.opencode.yml missing OpenCode policy marker: {marker}")

    plugin_source = (ROOT / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
    if "compatibility is resolved at image build" not in plugin_source.lower():
        fail("Awoki continuity plugin must document build-resolved compatibility")
    plugin_package = json.loads((ROOT / ".opencode" / "package.json").read_text(encoding="utf-8"))
    plugin_dependencies = plugin_package.get("dependencies") if isinstance(plugin_package.get("dependencies"), dict) else {}
    if plugin_dependencies.get("@opencode-ai/plugin") != "latest" or plugin_dependencies.get("@opencode-ai/sdk") != "latest":
        fail("source .opencode/package.json must follow latest; safe image builds materialize exact resolved versions")
    compat = (ROOT / ".harness" / "bin" / "opencode-runtime-compat-check").read_text(encoding="utf-8")
    for marker in ("resolved_cli", "@opencode-ai/plugin", "@opencode-ai/sdk", "mode=", "channel_state", "safe OpenCode version mismatch"):
        if marker not in compat:
            fail(f"OpenCode runtime compatibility gate missing marker: {marker}")

    entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(encoding="utf-8")
    if "/awoki/.harness/bin/opencode-runtime-compat-check" not in entrypoint:
        fail("OpenCode runtime compatibility gate must run again at SSH-container startup")

    image_contracts = (
        ("node_build_runtime", ("Dockerfile.opencode",)),
        ("python_runtime", ("Dockerfile", "Dockerfile.opencode")),
        ("go_semantics_builder", ("Dockerfile", "Dockerfile.opencode")),
    )
    for key, default_files in image_contracts:
        row = critical[key]
        image = str(row["image"])
        files = row.get("build_files") or ([row.get("build_file")] if row.get("build_file") else list(default_files))
        for filename in files:
            text = (ROOT / str(filename)).read_text(encoding="utf-8")
            if f"FROM {image}" not in text:
                fail(f"{filename} image for {key} does not match lock: {image}")

    qdrant_image = str(critical["qdrant_server"]["image"])
    qdrant_default = f"image: ${{AWOKI_QDRANT_IMAGE:-{qdrant_image}}}"
    for filename in ("docker-compose.yml", "docker-compose.opencode.yml"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        if qdrant_default not in text:
            fail(f"{filename} default Qdrant image does not match lock: {qdrant_image}")

    lavish_version = str(critical["lavish_axi"]["version"])
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    lavish_skill = (ROOT / ".opencode" / "skills" / "lavish-review" / "SKILL.md").read_text(encoding="utf-8")
    if f"AWOKI_LAVISH_VERSION={lavish_version}" not in env_example:
        fail(".env.example Lavish version does not match lock")
    if f"AWOKI_LAVISH_VERSION:-{lavish_version}" not in compose_opencode:
        fail("docker-compose.opencode.yml Lavish default does not match lock")
    if f'lavish-axi@"${{AWOKI_LAVISH_VERSION:-{lavish_version}}}"' not in lavish_skill:
        fail("lavish-review skill Lavish version does not match lock")

    requirements = [
        line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if requirements != list(lock.get("python_requirements") or []):
        fail("requirements.txt does not exactly match runtime-dependencies.lock.json")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for requirement in requirements:
        package = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip()
        if not package or not re.search(rf"(?im)^\s*[\"']?{re.escape(package)}(?:[<>=!~\[]|[\"'])", pyproject):
            fail(f"pyproject.toml does not represent requirement {requirement!r}")

    print(f"awoki_runtime_dependencies=ok opencode_policy=latest-or-safe plugin_sdk=match-resolved-cli lavish={lavish_version} qdrant={qdrant_image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
