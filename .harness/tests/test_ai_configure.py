from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONFIGURATOR = ROOT / ".harness" / "bin" / "awoki-ai-configure"
KEY_UPDATER = ROOT / ".harness" / "bin" / "awoki-ai-key-update"


class AIConfigurationTests(unittest.TestCase):
    def _compile(self, root: Path) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "AWOKI_ROOT": str(root),
            "AWOKI_AI_BASE_URL": "https://api.example.test/v1",
            "AWOKI_AI_API_KEY": "Bearer fake-offline-token",
            "AWOKI_AI_EMBEDDING_MODEL": "embedding-model",
            "AWOKI_AI_EMBEDDING_DEPLOYMENT": "embedding-model-r1",
            "AWOKI_AI_VECTOR_SIZE": "768",
            "AWOKI_AI_RERANK_MODEL": "reranker-model",
            "AWOKI_AI_CHAT_MODEL": "chat-model",
            "AWOKI_AI_PROVIDER_ID": "test-provider",
            "AWOKI_AI_PROVIDER_NAME": "Test Provider",
            "AWOKI_AI_MODEL_NAME": "Test Chat",
            "AWOKI_AI_CONTEXT": "210000",
            "AWOKI_AI_OUTPUT": "8192",
        }
        return subprocess.run(
            [sys.executable, str(CONFIGURATOR), "--non-interactive"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_compiler_generates_all_three_model_contracts_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / ".opencode-state" / "config" / "opencode.jsonc"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                '{\n  // preserve unrelated user settings\n  "theme": "system",\n}\n',
                encoding="utf-8",
            )

            completed = self._compile(root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("fake-offline-token", completed.stdout + completed.stderr)

            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("AWOKI_EMBEDDING_API_KEY=fake-offline-token\n", env_text)
            self.assertNotIn("AWOKI_EMBEDDING_API_KEY=Bearer", env_text)
            self.assertIn("AWOKI_EMBEDDING_MODEL=embedding-model\n", env_text)
            self.assertIn("AWOKI_EMBEDDING_DEPLOYMENT_ID=embedding-model-r1\n", env_text)
            self.assertIn("AWOKI_RERANK_MODEL=reranker-model\n", env_text)
            self.assertIn("AWOKI_RERANK_URL=https://api.example.test/rerank\n", env_text)
            self.assertIn("AWOKI_RERANK_API_KEY=\n", env_text)
            self.assertIn(
                "AWOKI_RERANK_API_KEY_ENV=AWOKI_EMBEDDING_API_KEY\n", env_text
            )

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["theme"], "system")
            provider = config["provider"]["test-provider"]
            self.assertEqual(provider["options"]["baseURL"], "https://api.example.test/v1")
            self.assertEqual(
                provider["options"]["apiKey"], "{env:AWOKI_EMBEDDING_API_KEY}"
            )
            self.assertEqual(
                provider["options"]["headers"]["Authorization"],
                "Bearer {env:AWOKI_EMBEDDING_API_KEY}",
            )
            self.assertEqual(provider["options"]["headers"]["User-Agent"], "awoki-runtime")
            self.assertIn("chat-model", provider["models"])
            self.assertEqual(provider["models"]["chat-model"]["limit"]["context"], 210000)

            embedding = runpy.run_path(str(ROOT / ".harness" / "bin" / "embedding-benchmark"))
            reranker = runpy.run_path(str(ROOT / ".harness" / "bin" / "reranker-benchmark"))
            expected = {
                "Content-Type": "application/json",
                "Authorization": "Bearer fake-offline-token",
                "User-Agent": "awoki-runtime",
            }
            self.assertEqual(embedding["_embedding_headers"]("fake-offline-token"), expected)
            self.assertEqual(reranker["_reranker_headers"]("fake-offline-token"), expected)

            add_env = {
                **os.environ,
                "AWOKI_ROOT": str(root),
                "AWOKI_AI_CHAT_MODEL": "chat-model-2",
                "AWOKI_AI_MODEL_NAME": "Second Chat",
                "AWOKI_AI_CONTEXT": "131072",
                "AWOKI_AI_OUTPUT": "4096",
            }
            added = subprocess.run(
                [
                    sys.executable,
                    str(CONFIGURATOR),
                    "--add-chat-model",
                    "--non-interactive",
                ],
                cwd=root,
                env=add_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            models = config["provider"]["test-provider"]["models"]
            self.assertEqual(set(models), {"chat-model", "chat-model-2"})
            self.assertEqual(models["chat-model"]["limit"]["context"], 210000)
            self.assertEqual(models["chat-model-2"]["name"], "Second Chat")
            self.assertEqual(models["chat-model-2"]["limit"]["context"], 131072)
            self.assertEqual(models["chat-model-2"]["limit"]["output"], 4096)

            bin_dir = root / ".harness" / "bin"
            bin_dir.mkdir(parents=True)
            shutil.copy2(CONFIGURATOR, bin_dir / CONFIGURATOR.name)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            make_log = root / "make.log"
            fake_make = fake_bin / "make"
            fake_make.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" > "$AWOKI_TEST_MAKE_LOG"\n',
                encoding="utf-8",
            )
            fake_make.chmod(0o755)
            env = {
                **os.environ,
                "AWOKI_ROOT": str(root),
                "AWOKI_TEST_MAKE_LOG": str(make_log),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            }
            rotated = subprocess.run(
                [str(KEY_UPDATER)],
                cwd=root,
                env=env,
                input="Bearer rotated-fake-token\n",
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(rotated.returncode, 0, rotated.stderr)
            self.assertNotIn("rotated-fake-token", rotated.stdout + rotated.stderr)
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("AWOKI_EMBEDDING_API_KEY=rotated-fake-token\n", env_text)
            self.assertNotIn("AWOKI_EMBEDDING_API_KEY=Bearer", env_text)
            self.assertIn("AWOKI_RERANK_API_KEY=\n", env_text)
            self.assertIn(
                "AWOKI_RERANK_API_KEY_ENV=AWOKI_EMBEDDING_API_KEY\n", env_text
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(config["provider"]["test-provider"]["models"]),
                {"chat-model", "chat-model-2"},
            )
            self.assertEqual(
                make_log.read_text(encoding="utf-8"),
                f"-C {root} opencode-config-reload\n",
            )

    def test_web_backend_imports_only_the_provider_key_from_runtime_snapshot(self) -> None:
        entrypoint = (ROOT / ".harness" / "bin" / "opencode-ssh-entrypoint").read_text(
            encoding="utf-8"
        )
        web_start = entrypoint[entrypoint.index("# Start the Web backend") :]
        self.assertIn('runtime_env_file=/run/awoki/runtime.env', web_start)
        self.assertIn('source "$runtime_env_file"', web_start)
        self.assertIn(
            "AWOKI_EMBEDDING_API_KEY|AWOKI_OPENCODE_WEB_PORT|AWOKI_OPENCODE_WEB_USERNAME",
            web_start,
        )
        self.assertIn('unset "$name"', web_start)
        self.assertIn('exec opencode web', web_start)

if __name__ == "__main__":
    unittest.main()
