from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTOR = ROOT / ".harness" / "bin" / "doctor"


class DoctorTests(unittest.TestCase):
    def _run_doctor(
        self,
        *,
        dotenv: str = "",
        compose_environment: str = "",
        compose_environment_status: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env_file = temp / ".env"
            env_file.write_text(dotenv, encoding="utf-8")
            compose_file = temp / "compose-environment.txt"
            compose_file.write_text(compose_environment, encoding="utf-8")
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            docker = fake_bin / "docker"
            docker.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    if [ "$1" = "compose" ] && [ "$2" = "version" ]; then
                      exit 0
                    fi
                    if [ "$1" = "compose" ] && [ "$2" = "config" ] && [ "$3" = "--environment" ]; then
                      cat "$FAKE_COMPOSE_ENV_FILE"
                      exit {compose_environment_status}
                    fi
                    if [ "$1" = "ps" ]; then
                      exit 0
                    fi
                    exit 1
                    """
                ),
                encoding="utf-8",
            )
            docker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join([str(fake_bin), env.get("PATH", "")])
            env["AWOKI_ENV_FILE"] = str(env_file)
            env["FAKE_COMPOSE_ENV_FILE"] = str(compose_file)
            for key in (
                "AWOKI_EMBEDDING_PROVIDER",
                "AWOKI_EMBEDDING_MODEL",
                "AWOKI_EMBEDDING_BASE_URL",
                "AWOKI_EMBEDDING_API_KEY",
                "AWOKI_VECTOR_SIZE",
                "AWOKI_OPENAI_BASE_URL",
                "AWOKI_OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
            ):
                env.pop(key, None)
            return subprocess.run(
                [str(DOCTOR)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_doctor_reads_embedding_configuration_from_dotenv(self) -> None:
        completed = self._run_doctor(
            dotenv=textwrap.dedent(
                """\
                AWOKI_EMBEDDING_PROVIDER=openai
                AWOKI_EMBEDDING_MODEL=text-embeddings-inference
                AWOKI_EMBEDDING_BASE_URL=http://embedding.example.invalid:8000/v1
                AWOKI_EMBEDDING_API_KEY=
                AWOKI_VECTOR_SIZE=768
                """
            ),
            compose_environment_status=1,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("remote embeddings configured", completed.stdout)
        self.assertIn("endpoint=http://embedding.example.invalid:8000/v1", completed.stdout)
        self.assertNotIn("remote embeddings are not configured", completed.stdout)

    def test_doctor_prefers_compose_resolved_environment(self) -> None:
        completed = self._run_doctor(
            compose_environment=textwrap.dedent(
                """\
                AWOKI_EMBEDDING_PROVIDER=openai
                AWOKI_EMBEDDING_MODEL=text-embeddings-inference
                AWOKI_EMBEDDING_BASE_URL=http://embedding.example.invalid:8000/v1
                AWOKI_VECTOR_SIZE=768
                """
            )
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("remote embeddings configured", completed.stdout)
        self.assertIn("model=text-embeddings-inference", completed.stdout)

    def test_doctor_warns_when_embedding_endpoint_and_key_are_missing(self) -> None:
        completed = self._run_doctor(compose_environment_status=1)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("remote embeddings are not configured", completed.stdout)


if __name__ == "__main__":
    unittest.main()
