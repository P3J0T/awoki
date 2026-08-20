from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / ".harness" / "bin" / "reconcile-opencode-port-owner"


class PortOwnerReconcileTests(unittest.TestCase):
    def _listener(self):
        proc = subprocess.Popen(
            [
                "python3",
                "-c",
                textwrap.dedent(
                    """
                    import socket, time
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("127.0.0.1", 0))
                    s.listen(1)
                    print(s.getsockname()[1], flush=True)
                    try:
                        time.sleep(60)
                    finally:
                        s.close()
                    """
                ),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        port = int(proc.stdout.readline().strip())
        self.addCleanup(lambda: proc.poll() is None and proc.terminate())
        return proc, port

    def _env(
        self,
        root: Path,
        port: int,
        listener_pid: int,
        *,
        workdir: str | None = None,
        runtime: str = "a" * 32,
        project: str = "awoki",
        service: str = "awoki-opencode-ssh",
        docker_owner: bool = True,
    ):
        bindir = root / "fake-bin"
        bindir.mkdir(parents=True, exist_ok=True)
        log = root / "docker.log"
        docker = bindir / "docker"
        docker.write_text(
            textwrap.dedent(
                r'''\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "$*" >> "$FAKE_DOCKER_LOG"
                if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
                if [[ "${1:-}" == "compose" && "$*" == *" ps -q "* ]]; then
                  exit 0
                fi
                if [[ "${1:-}" == "ps" ]]; then
                  if [[ "${FAKE_DOCKER_OWNER:-1}" == "1" ]]; then
                    printf '6327cf2c5f58\tawoki-awoki-opencode-ssh-1\t127.0.0.1:%s->22/tcp\n' "$FAKE_PORT"
                  fi
                  exit 0
                fi
                if [[ "${1:-}" == "inspect" ]]; then
                  template="${3:-}"
                  case "$template" in
                    *com.docker.compose.project.working_dir*) printf '%s\n' "$FAKE_WORKDIR" ;;
                    *com.docker.compose.service*) printf '%s\n' "$FAKE_SERVICE" ;;
                    *io.awoki.runtime_instance_id*) printf '%s\n' "$FAKE_RUNTIME" ;;
                    *com.docker.compose.project*) printf '%s\n' "$FAKE_PROJECT" ;;
                    *'.Name'*) printf '/awoki-awoki-opencode-ssh-1\n' ;;
                    *) exit 1 ;;
                  esac
                  exit 0
                fi
                if [[ "${1:-}" == "rm" && "${2:-}" == "-f" ]]; then
                  kill "$FAKE_LISTENER_PID" 2>/dev/null || true
                  exit 0
                fi
                exit 0
                '''
            ),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        compose = root / "docker-compose.opencode.yml"
        compose.write_text("services:\n  awoki-opencode-ssh:\n    image: awoki-opencode-ssh:latest\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                "AWOKI_ROOT": str(root),
                "AWOKI_RUNTIME_INSTANCE_ID": "b" * 32,
                "AWOKI_COMPOSE_PROJECT_NAME": "awoki",
                "AWOKI_RUNTIME_CONFLICT_POLICY": "auto",
                "FAKE_DOCKER_LOG": str(log),
                "FAKE_PORT": str(port),
                "FAKE_LISTENER_PID": str(listener_pid),
                "FAKE_WORKDIR": workdir or str(root),
                "FAKE_RUNTIME": runtime,
                "FAKE_PROJECT": project,
                "FAKE_SERVICE": service,
                "FAKE_DOCKER_OWNER": "1" if docker_owner else "0",
            }
        )
        return env, compose, log

    def test_stale_same_path_port_owner_is_removed_even_when_compose_ps_misses_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, log = self._env(root, port, listener.pid)
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            listener.wait(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "recovered")
            self.assertIn("held by stale Awoki", result.stderr)
            self.assertIn("rm -f 6327cf2c5f58", log.read_text(encoding="utf-8"))

    def test_docker_desktop_host_mnt_spelling_matches_same_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, _ = self._env(root, port, listener.pid, workdir="/host_mnt" + str(root))
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            listener.wait(timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "recovered")

    def test_current_runtime_owner_is_accepted_when_compose_ps_misses_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, log = self._env(root, port, listener.pid, runtime="b" * 32)
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "current")
            self.assertIn("did not enumerate it", result.stderr)
            self.assertNotIn("rm -f", log.read_text(encoding="utf-8"))

    def test_different_checkout_port_owner_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, log = self._env(root, port, listener.pid, workdir=str(root) + "-other")
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 5)
            self.assertIn("owned by another Awoki checkout", result.stderr)
            self.assertIn("Low-level startup will not stop or replace", result.stderr)
            self.assertNotIn("rm -f", log.read_text(encoding="utf-8"))

    def test_different_checkout_report_is_machine_readable_for_interactive_installer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            other = str(root) + "-other"
            env, compose, _ = self._env(root, port, listener.pid, workdir=other, project="old-awoki")
            report = root / "owner.tsv"
            env["AWOKI_PORT_OWNER_REPORT_FILE"] = str(report)
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 5, result.stderr)
            fields = report.read_text(encoding="utf-8").strip().split("\t")
            self.assertEqual(fields[0], "6327cf2c5f58")
            self.assertEqual(fields[2], "old-awoki")
            self.assertEqual(fields[3], "awoki-opencode-ssh")
            self.assertEqual(fields[4], os.path.realpath(other))
            self.assertEqual(fields[6], str(port))

    def test_non_docker_listener_fails_with_lsof_hint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, _ = self._env(root, port, listener.pid, docker_owner=False)
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 3)
            self.assertIn("non-Docker process", result.stderr)
            self.assertIn(f"lsof -nP -iTCP:{port}", result.stderr)

    def test_ask_policy_without_tty_never_removes_stale_owner(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listener, port = self._listener()
            env, compose, log = self._env(root, port, listener.pid)
            env["AWOKI_RUNTIME_CONFLICT_POLICY"] = "ask"
            result = subprocess.run([str(HELPER), str(compose), str(port), "SSH", "awoki-opencode-ssh"], env=env, text=True, capture_output=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 3)
            self.assertIn("no interactive terminal", result.stderr)
            self.assertNotIn("rm -f", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
