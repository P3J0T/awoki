from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / ".harness" / "bin" / "verify-opencode-ssh-client"


def _make_keypair(root: Path, key_material: str = "AAAATEST") -> tuple[Path, Path]:
    ssh_dir = root / ".ssh-container"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    private = ssh_dir / "id_ed25519"
    public = ssh_dir / "id_ed25519.pub"
    private.write_text("fake-private-key\n", encoding="utf-8")
    public.write_text(f"ssh-ed25519 {key_material} awoki-opencode\n", encoding="utf-8")
    Path(str(private) + ".derived").write_text(
        f"ssh-ed25519 {key_material}\n", encoding="utf-8"
    )
    return private, public


def _fake_tools(root: Path, authorized_keys: Path, ssh_exit: int = 0) -> tuple[Path, Path]:
    bindir = root / "fake-bin"
    bindir.mkdir()
    docker_log = root / "docker.log"
    ssh_log = root / "ssh.log"

    docker = bindir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
if [[ "${1:-}" == "compose" && "$*" == *" ps -q awoki-opencode-ssh"* ]]; then echo fake-container; exit 0; fi
if [[ "${1:-}" == "inspect" ]]; then echo true; exit 0; fi
if [[ "${1:-}" == "exec" && "${2:-}" == "fake-container" && "${3:-}" == "cat" ]]; then
  cat "$FAKE_AUTHORIZED_KEYS"
  exit 0
fi
exit 99
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    ssh = bindir / "ssh"
    ssh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
exit "${FAKE_SSH_EXIT:-0}"
""",
        encoding="utf-8",
    )
    ssh.chmod(0o755)

    ssh_keygen = bindir / "ssh-keygen"
    ssh_keygen.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-y" && "${2:-}" == "-f" ]]; then
  cat "$3.derived"
  exit 0
fi
if [[ "${1:-}" == "-lf" ]]; then
  test -s "$2"
  echo '256 SHA256:fake awoki-opencode (ED25519)'
  exit 0
fi
exit 99
""",
        encoding="utf-8",
    )
    ssh_keygen.chmod(0o755)

    return docker_log, ssh_log


def _run(root: Path, authorized_keys: Path, ssh_exit: int = 0) -> subprocess.CompletedProcess[str]:
    docker_log, ssh_log = _fake_tools(root, authorized_keys, ssh_exit)
    compose = root / "docker-compose.opencode.yml"
    compose.write_text("services:\n  awoki-opencode-ssh:\n    image: test\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "AWOKI_ROOT": str(root),
            "AWOKI_SSH_VERIFY_ATTEMPTS": "1",
            "AWOKI_SSH_VERIFY_DELAY_SECONDS": "0",
            "FAKE_AUTHORIZED_KEYS": str(authorized_keys),
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_SSH_LOG": str(ssh_log),
            "FAKE_SSH_EXIT": str(ssh_exit),
            "PATH": f"{root / 'fake-bin'}:{env['PATH']}",
        }
    )
    return subprocess.run(
        [str(HELPER)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_ssh_client_verifier_accepts_matching_host_and_container_key(tmp_path: Path) -> None:
    private, public = _make_keypair(tmp_path)
    completed = _run(tmp_path, public)
    assert completed.returncode == 0, completed.stderr
    assert "awoki_opencode_ssh_client=ok" in completed.stdout
    ssh_log = (tmp_path / "ssh.log").read_text(encoding="utf-8")
    assert f"-i {private}" in ssh_log
    assert "-o BatchMode=yes" in ssh_log
    assert "-o IdentitiesOnly=yes" in ssh_log


def test_ssh_client_verifier_rejects_missing_private_key(tmp_path: Path) -> None:
    private, public = _make_keypair(tmp_path)
    private.unlink()
    completed = _run(tmp_path, public)
    assert completed.returncode == 3
    assert "host SSH private key is missing/empty" in completed.stderr


def test_ssh_client_verifier_rejects_container_authorized_key_mismatch(tmp_path: Path) -> None:
    _make_keypair(tmp_path, "AAAAHOST")
    other_root = tmp_path / "other"
    _, other_public = _make_keypair(other_root, "AAAACONTAINER")
    completed = _run(tmp_path, other_public)
    assert completed.returncode == 3
    assert "authorized for a different SSH key" in completed.stderr


def test_ssh_client_verifier_rejects_failed_real_login(tmp_path: Path) -> None:
    _, public = _make_keypair(tmp_path)
    completed = _run(tmp_path, public, ssh_exit=255)
    assert completed.returncode == 3
    assert "public-key BatchMode login did not succeed" in completed.stderr
