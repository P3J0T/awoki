from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.harness' / 'bin' / 'prepare-qdrant-storage'
WAIT_SCRIPT = ROOT / '.harness' / 'bin' / 'wait-qdrant'
RECONCILE_SCRIPT = ROOT / '.harness' / 'bin' / 'reconcile-opencode-runtime'


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



    def test_init_layout_runtime_instance_is_stable_until_marker_is_recreated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bindir = root / '.harness' / 'bin'
            bindir.mkdir(parents=True)
            for source in (ROOT / '.harness' / 'bin' / 'init-layout', ROOT / '.harness' / 'bin' / 'prepare-qdrant-storage'):
                target = bindir / source.name
                target.write_bytes(source.read_bytes())
                target.chmod(0o755)
            fake_keys = bindir / 'prepare-opencode-ssh-keys'
            fake_keys.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            fake_keys.chmod(0o755)
            env = os.environ.copy()
            env['AWOKI_ROOT'] = str(root)
            first = subprocess.run([str(bindir / 'init-layout')], env=env, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            marker = root / '.harness' / 'state' / 'layout_initialized.json'
            first_id = json.loads(marker.read_text(encoding='utf-8'))['runtime_instance_id']
            self.assertRegex(first_id, r'^[0-9a-f]{32}$')

            second = subprocess.run([str(bindir / 'init-layout')], env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_id = json.loads(marker.read_text(encoding='utf-8'))['runtime_instance_id']
            self.assertEqual(second_id, first_id)

            marker.unlink()
            third = subprocess.run([str(bindir / 'init-layout')], env=env, capture_output=True, text=True)
            self.assertEqual(third.returncode, 0, third.stderr)
            third_id = json.loads(marker.read_text(encoding='utf-8'))['runtime_instance_id']
            self.assertRegex(third_id, r'^[0-9a-f]{32}$')
            self.assertNotEqual(third_id, first_id)

    def _fake_reconcile_env(self, root: Path, *, working_dir: str, instance_id: str):
        bindir = root / 'fake-reconcile-bin'
        bindir.mkdir()
        log = root / 'reconcile-docker.log'
        fake = bindir / 'docker'
        fake.write_text(textwrap.dedent('''\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "$FAKE_DOCKER_LOG"
            if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then exit 0; fi
            qdrant_full=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
            opencode_full=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
            if [[ "${1:-}" == "compose" && "$*" == *" ps -a -q qdrant"* ]]; then
              if [[ "${FAKE_COMPOSE_VISIBLE:-1}" == "1" ]]; then
                if [[ "${FAKE_REVERSED_ID_FORMS:-0}" == "1" ]]; then
                  echo "${qdrant_full:0:12}"
                elif [[ "${FAKE_MIXED_ID_FORMS:-0}" == "1" ]]; then
                  echo "$qdrant_full"
                else
                  echo fake-qdrant
                fi
              fi
              exit 0
            fi
            if [[ "${1:-}" == "compose" && "$*" == *" ps -a -q awoki-opencode-ssh"* ]]; then
              if [[ "${FAKE_COMPOSE_VISIBLE:-1}" == "1" ]]; then
                if [[ "${FAKE_REVERSED_ID_FORMS:-0}" == "1" ]]; then
                  echo "${opencode_full:0:12}"
                elif [[ "${FAKE_MIXED_ID_FORMS:-0}" == "1" ]]; then
                  echo "$opencode_full"
                else
                  echo fake-opencode
                fi
              fi
              exit 0
            fi
            if [[ "${1:-}" == "ps" && "$*" == *"label=com.docker.compose.service=qdrant"* ]]; then
              if [[ "${FAKE_REVERSED_ID_FORMS:-0}" == "1" ]]; then
                echo "$qdrant_full"
              elif [[ "${FAKE_MIXED_ID_FORMS:-0}" == "1" ]]; then
                echo "${qdrant_full:0:12}"
              else
                echo fake-qdrant
              fi
              exit 0
            fi
            if [[ "${1:-}" == "ps" && "$*" == *"label=com.docker.compose.service=awoki-opencode-ssh"* ]]; then
              if [[ "${FAKE_REVERSED_ID_FORMS:-0}" == "1" ]]; then
                echo "$opencode_full"
              elif [[ "${FAKE_MIXED_ID_FORMS:-0}" == "1" ]]; then
                echo "${opencode_full:0:12}"
              else
                echo fake-opencode
              fi
              exit 0
            fi
            if [[ "${1:-}" == "inspect" ]]; then
              template="${3:-}"
              cid="${4:-}"
              if [[ "$template" == *".Id"* ]]; then
                case "$cid" in
                  "$qdrant_full"|"${qdrant_full:0:12}") echo "$qdrant_full" ;;
                  "$opencode_full"|"${opencode_full:0:12}") echo "$opencode_full" ;;
                  *) echo "$cid" ;;
                esac
              elif [[ "$template" == *"project.working_dir"* ]]; then echo "$FAKE_RUNTIME_WORKDIR"
              elif [[ "$template" == *"io.awoki.runtime_instance_id"* ]]; then echo "$FAKE_RUNTIME_INSTANCE"
              elif [[ "$template" == *".Name"* ]]; then echo "/$cid"
              fi
              exit 0
            fi
            if [[ "${1:-}" == "compose" && "$*" == *" down --remove-orphans"* ]]; then exit "${FAKE_DOWN_STATUS:-0}"; fi
            exit 0
        '''), encoding='utf-8')
        fake.chmod(0o755)
        compose = root / 'docker-compose.opencode.yml'
        compose.write_text('services:\n  qdrant:\n    image: qdrant/qdrant:v1.18.2\n  awoki-opencode-ssh:\n    image: awoki-opencode-ssh:latest\n', encoding='utf-8')
        env = os.environ.copy()
        env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
        env['AWOKI_ROOT'] = str(root)
        env['AWOKI_RUNTIME_INSTANCE_ID'] = 'b' * 32
        env['FAKE_DOCKER_LOG'] = str(log)
        env['FAKE_RUNTIME_WORKDIR'] = working_dir
        env['FAKE_RUNTIME_INSTANCE'] = instance_id
        env['FAKE_DOWN_STATUS'] = '0'
        env['FAKE_COMPOSE_VISIBLE'] = '1'
        return env, compose, log

    def test_reconcile_same_path_stale_runtime_explains_and_removes_without_volumes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='a' * 32)
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Detected stale Awoki containers', result.stderr)
            self.assertIn('Docker Desktop can retain bind mounts', result.stderr)
            self.assertIn('Persistent named volumes and host data', result.stderr)
            docker_log = log.read_text(encoding='utf-8')
            self.assertIn('rm -f fake-qdrant', docker_log)
            self.assertIn('rm -f fake-opencode', docker_log)
            self.assertNotIn('rm -v', docker_log)
            self.assertNotIn('down --remove-orphans', docker_log)


    def test_reconcile_deduplicates_short_and_full_container_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='a' * 32)
            env['FAKE_MIXED_ID_FORMS'] = '1'
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            docker_log = log.read_text(encoding='utf-8')
            qdrant_full = 'a' * 64
            opencode_full = 'b' * 64
            self.assertEqual(docker_log.count(f'rm -f {qdrant_full}'), 1)
            self.assertEqual(docker_log.count(f'rm -f {opencode_full}'), 1)
            self.assertEqual(result.stderr.count('[awoki]   qdrant:'), 1)
            self.assertEqual(result.stderr.count('[awoki]   awoki-opencode-ssh:'), 1)

    def test_reconcile_canonicalizes_compose_membership_when_compose_uses_short_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = str(root) + '-other'
            env, compose, log = self._fake_reconcile_env(
                root,
                working_dir=other,
                instance_id='a' * 32,
            )
            env['FAKE_REVERSED_ID_FORMS'] = '1'
            result = subprocess.run(
                [str(RECONCILE_SCRIPT), str(compose)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn('Compose project conflict', result.stderr)
            self.assertNotIn('rm -f', log.read_text(encoding='utf-8'))

    def test_reconcile_finds_same_path_stale_runtime_even_when_compose_ps_cannot_see_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='a' * 32)
            env['FAKE_COMPOSE_VISIBLE'] = '0'
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Detected stale Awoki containers', result.stderr)
            docker_log = log.read_text(encoding='utf-8')
            self.assertIn('ps -a --filter label=com.docker.compose.service=qdrant -q', docker_log)
            self.assertIn('ps -a --filter label=com.docker.compose.service=awoki-opencode-ssh -q', docker_log)
            self.assertIn('rm -f fake-qdrant', docker_log)
            self.assertIn('rm -f fake-opencode', docker_log)

    def test_reconcile_legacy_same_path_runtime_is_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='<no value>')
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('legacy/missing', result.stderr)
            self.assertIn('rm -f fake-qdrant', log.read_text(encoding='utf-8'))
            self.assertIn('rm -f fake-opencode', log.read_text(encoding='utf-8'))

    def test_reconcile_fail_policy_explains_stale_runtime_without_removing_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='a' * 32)
            env['AWOKI_RUNTIME_CONFLICT_POLICY'] = 'fail'
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertIn('conflict policy is fail', result.stderr)
            self.assertNotIn('rm -f fake-qdrant', log.read_text(encoding='utf-8'))
            self.assertNotIn('rm -f fake-opencode', log.read_text(encoding='utf-8'))

    def test_reconcile_ask_policy_without_tty_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id='a' * 32)
            env['AWOKI_RUNTIME_CONFLICT_POLICY'] = 'ask'
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertIn('no interactive terminal is available', result.stderr)
            self.assertNotIn('rm -f fake-qdrant', log.read_text(encoding='utf-8'))
            self.assertNotIn('rm -f fake-opencode', log.read_text(encoding='utf-8'))

    def test_reconcile_matching_runtime_does_not_remove_containers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = 'b' * 32
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(root), instance_id=current)
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('rm -f fake-qdrant', log.read_text(encoding='utf-8'))
            self.assertNotIn('rm -f fake-opencode', log.read_text(encoding='utf-8'))
            self.assertEqual(result.stderr, '')

    def test_reconcile_different_checkout_refuses_automatic_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = root.parent / (root.name + '-other')
            env, compose, log = self._fake_reconcile_env(root, working_dir=str(other), instance_id='a' * 32)
            result = subprocess.run([str(RECONCILE_SCRIPT), str(compose)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 4)
            self.assertIn('belongs to a different checkout', result.stderr)
            self.assertIn('Refusing automatic cleanup', result.stderr)
            self.assertNotIn('rm -f fake-qdrant', log.read_text(encoding='utf-8'))
            self.assertNotIn('rm -f fake-opencode', log.read_text(encoding='utf-8'))

    def test_launcher_reconciles_runtime_before_qdrant_bind_probe(self):
        launcher = (ROOT / '.harness' / 'bin' / 'run-opencode-ssh').read_text(encoding='utf-8')
        reconcile = '"$ROOT/.harness/bin/reconcile-opencode-runtime" "$COMPOSE_FILE"'
        preflight = '"$ROOT/.harness/bin/prepare-qdrant-storage" "$COMPOSE_FILE"'
        self.assertIn(reconcile, launcher)
        self.assertIn(preflight, launcher)
        self.assertLess(launcher.index(reconcile), launcher.index(preflight))
        init_layout = (ROOT / '.harness' / 'bin' / 'init-layout').read_text(encoding='utf-8')
        self.assertIn('runtime_instance_id', init_layout)
        self.assertIn('secrets.token_hex(16)', init_layout)

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
                if [[ "${1:-}" == "compose" && "$*" == *" run "* ]]; then
                  [[ " $* " == *" -T "* ]] || exit 64
                  exit "${FAKE_DOCKER_RUN_STATUS:-0}"
                fi
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
            self.assertIn(f'compose -f {compose} run -T --rm --no-deps --entrypoint python3 awoki-opencode-ssh - 1', docker_log)
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
