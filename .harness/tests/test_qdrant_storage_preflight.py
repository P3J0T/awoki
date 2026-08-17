from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.harness' / 'bin' / 'prepare-qdrant-storage'
WAIT_SCRIPT = ROOT / '.harness' / 'bin' / 'wait-qdrant'


class QdrantStoragePreflightTests(unittest.TestCase):
    def test_host_only_materializes_collections_without_docker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '.harness' / 'bin').mkdir(parents=True)
            target = root / '.harness' / 'bin' / 'prepare-qdrant-storage'
            target.write_bytes(SCRIPT.read_bytes())
            target.chmod(0o755)
            env = os.environ.copy()
            env['AWOKI_ROOT'] = str(root)
            env['AWOKI_QDRANT_STORAGE_HOST_ONLY'] = '1'
            subprocess.run([str(target)], env=env, check=True, capture_output=True, text=True)
            self.assertTrue((root / 'data' / 'qdrant' / 'collections').is_dir())
            self.assertEqual(list((root / 'data' / 'qdrant' / 'collections').iterdir()), [])

    def _fake_docker_env(self, root: Path, *, working_dir: str | None = None, exec_status: int = 0):
        bindir = root / 'fake-bin'
        bindir.mkdir()
        log = root / 'docker.log'
        fake = bindir / 'docker'
        fake.write_text(textwrap.dedent('''\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "$FAKE_DOCKER_LOG"
            if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
            if [[ "${1:-}" == "compose" && "$*" == *" ps -a -q qdrant"* ]]; then echo fake-qdrant; exit 0; fi
            if [[ "${1:-}" == "inspect" ]]; then
              template="${3:-}"
              if [[ "$template" == *".State.Running"* ]]; then echo true
              elif [[ "$template" == *"project.working_dir"* ]]; then echo "${FAKE_QDRANT_WORKDIR}"
              elif [[ "$template" == *"/qdrant/storage"* ]]; then echo "${FAKE_QDRANT_MOUNT}"
              fi
              exit 0
            fi
            if [[ "${1:-}" == "exec" ]]; then exit "${FAKE_DOCKER_EXEC_STATUS:-0}"; fi
            if [[ "${1:-}" == "compose" && "$*" == *" run "* ]]; then exit 0; fi
            exit 0
        '''), encoding='utf-8')
        fake.chmod(0o755)
        compose = root / 'docker-compose.opencode.yml'
        compose.write_text('services:\n  qdrant:\n    image: qdrant/qdrant:v1.18.2\n', encoding='utf-8')
        env = os.environ.copy()
        env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
        env['AWOKI_ROOT'] = str(root)
        env['AWOKI_QDRANT_STORAGE_LIVE_ONLY'] = '1'
        env['FAKE_DOCKER_LOG'] = str(log)
        env['FAKE_QDRANT_WORKDIR'] = working_dir or str(root)
        env['FAKE_QDRANT_MOUNT'] = str(root / 'data' / 'qdrant')
        env['FAKE_DOCKER_EXEC_STATUS'] = str(exec_status)
        return env, compose, log

    def test_live_probe_checks_actual_running_qdrant_container(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_docker_env(root)
            result = subprocess.run([str(SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('exec --user 0:0 fake-qdrant', log.read_text(encoding='utf-8'))

    def test_live_probe_rejects_qdrant_from_previous_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = root.parent / (root.name + '-old-checkout')
            env, compose, _ = self._fake_docker_env(root, working_dir=str(other))
            result = subprocess.run([str(SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('storage identity mismatch', result.stderr)
            self.assertIn('another Awoki checkout', result.stderr)

    def test_live_probe_fails_closed_when_running_mount_is_not_writable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, _ = self._fake_docker_env(root, exec_status=1)
            result = subprocess.run([str(SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('running Qdrant storage is not writable', result.stderr)

    def test_startup_orders_qdrant_before_opencode_and_live_probe(self):
        launcher = (ROOT / '.harness' / 'bin' / 'run-opencode-ssh').read_text(encoding='utf-8')
        preflight = '"$ROOT/.harness/bin/prepare-qdrant-storage" "$COMPOSE_FILE"'
        qdrant_up = 'docker compose -f "$COMPOSE_FILE" up -d qdrant'
        wait = 'AWOKI_QDRANT_WAIT_SERVICE=awoki-opencode-ssh'
        live = 'AWOKI_QDRANT_STORAGE_LIVE_ONLY=1'
        opencode = 'opencode_up+=(awoki-opencode-ssh)'
        for marker in (preflight, qdrant_up, wait, live, opencode):
            self.assertIn(marker, launcher)
        self.assertLess(launcher.index(preflight), launcher.index(qdrant_up))
        self.assertLess(launcher.index(qdrant_up), launcher.index(wait))
        self.assertLess(launcher.index(wait), launcher.index(live))
        self.assertLess(launcher.index(live), launcher.index(opencode))

    def test_wait_qdrant_internal_probe_uses_compose_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = root / 'fake-bin'
            bindir.mkdir()
            log = root / 'docker.log'
            fake = bindir / 'docker'
            fake.write_text(textwrap.dedent('''\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "$*" >> "$FAKE_DOCKER_LOG"
                if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
                if [[ "${1:-}" == "compose" && "$*" == *" run "* ]]; then exit "${FAKE_DOCKER_RUN_STATUS:-0}"; fi
                if [[ "${1:-}" == "compose" && "$*" == *" logs "* ]]; then exit 0; fi
                exit 0
            '''), encoding='utf-8')
            fake.chmod(0o755)
            compose = root / 'docker-compose.opencode.yml'
            compose.write_text('services:\n  qdrant:\n    image: qdrant/qdrant:v1.18.2\n  awoki-opencode-ssh:\n    image: awoki-opencode-ssh:latest\n', encoding='utf-8')
            env = os.environ.copy()
            env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
            env['AWOKI_ROOT'] = str(root)
            env['AWOKI_QDRANT_WAIT_COMPOSE_FILE'] = str(compose)
            env['AWOKI_QDRANT_WAIT_SERVICE'] = 'awoki-opencode-ssh'
            env['AWOKI_QDRANT_WAIT_SECONDS'] = '1'
            env['FAKE_DOCKER_LOG'] = str(log)
            env['FAKE_DOCKER_RUN_STATUS'] = '0'
            result = subprocess.run([str(WAIT_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            docker_log = log.read_text(encoding='utf-8')
            self.assertIn('compose version', docker_log)
            self.assertIn(f'compose -f {compose} run --rm --no-deps --entrypoint python3 awoki-opencode-ssh - 1', docker_log)
            self.assertIn('qdrant is ready on Docker network at http://qdrant:6333', result.stderr)

    def test_wait_qdrant_internal_probe_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = root / 'fake-bin'
            bindir.mkdir()
            log = root / 'docker.log'
            fake = bindir / 'docker'
            fake.write_text(textwrap.dedent('''\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "$*" >> "$FAKE_DOCKER_LOG"
                if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
                if [[ "${1:-}" == "compose" && "$*" == *" run "* ]]; then exit 1; fi
                if [[ "${1:-}" == "compose" && "$*" == *" logs "* ]]; then exit 0; fi
                exit 0
            '''), encoding='utf-8')
            fake.chmod(0o755)
            compose = root / 'docker-compose.opencode.yml'
            compose.write_text('services:\n  qdrant:\n    image: qdrant/qdrant:v1.18.2\n  awoki-opencode-ssh:\n    image: awoki-opencode-ssh:latest\n', encoding='utf-8')
            env = os.environ.copy()
            env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
            env['AWOKI_ROOT'] = str(root)
            env['AWOKI_QDRANT_WAIT_COMPOSE_FILE'] = str(compose)
            env['AWOKI_QDRANT_WAIT_SERVICE'] = 'awoki-opencode-ssh'
            env['AWOKI_QDRANT_WAIT_SECONDS'] = '1'
            env['FAKE_DOCKER_LOG'] = str(log)
            result = subprocess.run([str(WAIT_SCRIPT)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('did not become ready on Docker network', result.stderr)
            self.assertIn('logs --tail=100 qdrant', log.read_text(encoding='utf-8'))

    def test_recreate_delegates_force_recreate_to_verified_launcher(self):
        recreate = (ROOT / '.harness' / 'bin' / 'recreate-opencode-runtime').read_text(encoding='utf-8')
        self.assertIn('AWOKI_OPENCODE_FORCE_RECREATE=1', recreate)
        self.assertIn('"$ROOT/.harness/bin/run-opencode-ssh"', recreate)

    def test_default_preflight_still_uses_current_compose_bind_probe(self):
        text = SCRIPT.read_text(encoding='utf-8')
        self.assertIn('docker compose -f "$COMPOSE_FILE" run --rm --no-deps', text)
        self.assertIn('--user 0:0', text)
        self.assertIn('mkdir -p "$collections"', text)
        init_layout = (ROOT / '.harness' / 'bin' / 'init-layout').read_text(encoding='utf-8')
        self.assertIn('AWOKI_QDRANT_STORAGE_HOST_ONLY=1 "$ROOT/.harness/bin/prepare-qdrant-storage"', init_layout)


if __name__ == '__main__':
    unittest.main()
