from __future__ import annotations

import contextlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backup


@contextlib.contextmanager
def env(**updates):
    old = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_root(base: Path, name: str = "awoki") -> Path:
    root = base / name
    (root / ".harness" / "state").mkdir(parents=True)
    (root / ".harness" / "artifacts").mkdir(parents=True)
    (root / ".harness" / "memory").mkdir(parents=True)
    (root / ".harness" / "index").mkdir(parents=True)
    (root / "workspace" / "projects" / "demo" / "memory").mkdir(parents=True)
    (root / "workspace" / "projects" / "demo" / "index" / "sqlite").mkdir(parents=True)
    (root / "data" / "qdrant").mkdir(parents=True)
    (root / "workspace" / ".lavish" / "state").mkdir(parents=True)
    (root / ".awoki-global" / "global").mkdir(parents=True)
    (root / ".awoki-global" / "skills").mkdir(parents=True)
    (root / ".opencode-state" / "share").mkdir(parents=True)
    (root / ".opencode-state" / "cache").mkdir(parents=True)
    (root / ".ssh-container").mkdir(parents=True)

    (root / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl").write_text(
        '{"kind":"finding","summary":"canonical"}\n', encoding="utf-8"
    )
    (root / "workspace" / "projects" / "demo" / "project.json").write_text(
        '{"project_id":"demo"}\n', encoding="utf-8"
    )
    (root / "workspace" / "projects" / "demo" / "index" / "sqlite" / "awoki_project_fts.sqlite").write_bytes(b"derived-project-index")
    (root / ".harness" / "state" / "last_project.json").write_text('{"project_id":"demo"}\n', encoding="utf-8")
    (root / ".harness" / "state" / "layout_initialized.json").write_text('{"old":"absolute"}\n', encoding="utf-8")
    (root / ".harness" / "artifacts" / "evidence.txt").write_text("evidence", encoding="utf-8")
    (root / ".harness" / "state" / "README.md").write_text("tracked state documentation\n", encoding="utf-8")
    (root / ".harness" / "state" / "nested").mkdir()
    (root / ".harness" / "state" / "nested" / "README.md").write_text("user state documentation\n", encoding="utf-8")
    (root / ".harness" / "artifacts" / "evidence").mkdir()
    (root / ".harness" / "artifacts" / "evidence" / "README.md").write_text("tracked artifact documentation\n", encoding="utf-8")
    (root / ".harness" / "artifacts" / "custom").mkdir()
    (root / ".harness" / "artifacts" / "custom" / "README.md").write_text("user artifact documentation\n", encoding="utf-8")
    (root / ".harness" / "memory" / "promotion_candidates.jsonl").write_text("", encoding="utf-8")
    (root / ".harness" / "index" / "awoki_project_fts.sqlite").write_bytes(b"legacy-index")
    (root / ".harness" / "notes.md").write_text("notes", encoding="utf-8")
    (root / ".awoki-global" / "global" / "memories.jsonl").write_text('{"text":"global"}\n', encoding="utf-8")
    (root / ".awoki-global" / "global" / "awoki_global_fts.sqlite").write_bytes(b"global-index")
    (root / ".awoki-global" / "archive").mkdir()
    (root / ".awoki-global" / "archive" / "index-manifest.json").write_text("canonical nested file\n", encoding="utf-8")
    (root / ".awoki-global" / "skills" / "custom.md").write_text("skill", encoding="utf-8")
    (root / "data" / "qdrant" / "collection.bin").write_bytes(b"qdrant")
    (root / "workspace" / ".lavish" / "state" / "session.json").write_text("lavish-runtime", encoding="utf-8")
    (root / ".opencode-state" / "share" / "session.json").write_text("session", encoding="utf-8")
    (root / ".opencode-state" / "cache" / "cache.bin").write_bytes(b"cache")
    (root / ".ssh-container" / "id_ed25519").write_text("private-key", encoding="utf-8")
    (root / ".env").write_text(
        "AWOKI_GLOBAL_ROOT=" + str(root / ".awoki-global") + "\n"
        "AWOKI_GLOBAL_SKILLS_DIR=" + str(root / ".awoki-global" / "skills") + "\n"
        "AWOKI_EMBEDDING_PROVIDER=openai\n"
        "AWOKI_EMBEDDING_MODEL=text-embeddings-inference\n"
        "AWOKI_EMBEDDING_DEPLOYMENT_ID=jinaai/jina-embeddings-v2-base-code@test-revision\n"
        "AWOKI_EMBEDDING_NORMALIZE=1\n"
        "AWOKI_VECTOR_SIZE=768\n"
        "AWOKI_QDRANT_COLLECTION=awoki_jina_embeddings_v2_base_code_768\n",
        encoding="utf-8",
    )
    init = root / "init-awoki.sh"
    init.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        "mkdir -p \"$AWOKI_ROOT/workspace\" \"$AWOKI_ROOT/.harness/state\" \"$AWOKI_GLOBAL_ROOT/global\"\n"
        "printf '{\\\"status\\\":\\\"initialized\\\"}\\n' > \"$AWOKI_ROOT/.harness/state/layout_initialized.json\"\n",
        encoding="utf-8",
    )
    init.chmod(0o755)
    return root


def archive_names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as tar:
        return {member.name for member in tar.getmembers()}


class BackupTests(unittest.TestCase):
    def test_portable_backup_keeps_canonical_data_and_excludes_derived_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            out = base / "backups"
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global", AWOKI_GLOBAL_SKILLS_DIR=root / ".awoki-global" / "skills"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.create_backup(root, mode="portable", output_dir=out)
            archive = Path(result["archive"])
            names = archive_names(archive)
            self.assertIn("awoki-backup/payload/workspace/projects/demo/memory/continuity.jsonl", names)
            self.assertIn("awoki-backup/payload/global_repo/global/memories.jsonl", names)
            self.assertIn("awoki-backup/payload/harness_state/nested/README.md", names)
            self.assertIn("awoki-backup/payload/harness_artifacts/custom/README.md", names)
            self.assertIn("awoki-backup/payload/global_repo/archive/index-manifest.json", names)
            self.assertNotIn("awoki-backup/payload/workspace/projects/demo/index/sqlite/awoki_project_fts.sqlite", names)
            self.assertNotIn("awoki-backup/payload/global_repo/global/awoki_global_fts.sqlite", names)
            self.assertFalse(any("qdrant" in name for name in names))
            self.assertFalse(any("dotenv" in name for name in names))
            self.assertFalse(any("opencode_state" in name for name in names))
            self.assertFalse(any("ssh_container" in name for name in names))
            self.assertFalse(any(name.endswith("layout_initialized.json") for name in names))
            self.assertNotIn("awoki-backup/payload/harness_artifacts/evidence/README.md", names)
            self.assertNotIn("awoki-backup/payload/harness_state/README.md", names)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertTrue(Path(str(archive) + ".sha256").exists())

    def test_full_backup_adds_indexes_and_qdrant_but_not_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global", AWOKI_GLOBAL_SKILLS_DIR=root / ".awoki-global" / "skills"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.create_backup(root, mode="full", output_dir=base / "backups")
            names = archive_names(Path(result["archive"]))
            self.assertIn("awoki-backup/payload/workspace/projects/demo/index/sqlite/awoki_project_fts.sqlite", names)
            self.assertIn("awoki-backup/payload/harness_index/awoki_project_fts.sqlite", names)
            self.assertIn("awoki-backup/payload/qdrant/collection.bin", names)
            self.assertFalse(any("workspace/.lavish/state" in name for name in names))
            self.assertFalse(any("dotenv" in name for name in names))
            self.assertFalse(any("opencode_state" in name for name in names))

    def test_explicit_sensitive_options_include_env_ssh_and_noncache_opencode_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global", AWOKI_GLOBAL_SKILLS_DIR=root / ".awoki-global" / "skills"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.create_backup(
                        root,
                        mode="full",
                        output_dir=base / "backups",
                        include_opencode_state=True,
                        include_secrets=True,
                    )
            names = archive_names(Path(result["archive"]))
            self.assertIn("awoki-backup/payload/dotenv", names)
            self.assertIn("awoki-backup/payload/ssh_container/id_ed25519", names)
            self.assertIn("awoki-backup/payload/opencode_state/share/session.json", names)
            self.assertFalse(any("opencode_state/cache" in name for name in names))

    def test_verify_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.create_backup(root, mode="portable", output_dir=base / "backups")
            archive = Path(result["archive"])
            verified = backup.verify_backup(archive)
            self.assertEqual(verified["status"], "verified")
            with archive.open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaises(backup.BackupError):
                backup.verify_backup(archive)

    def test_full_backup_refuses_live_qdrant_even_when_live_capture_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            running = [{"compose_file": "docker-compose.yml", "service": "qdrant"}]
            with mock.patch.object(backup, "_running_compose_services", return_value=running):
                with self.assertRaisesRegex(backup.BackupError, "raw Qdrant storage may never be copied live"):
                    backup.create_backup(root, mode="full", output_dir=base / "backups", allow_live=True)

    def test_portable_restore_maps_data_and_invalidates_derived_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global", AWOKI_GLOBAL_SKILLS_DIR=source / ".awoki-global" / "skills"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")

            destination = make_root(base, "destination")
            # Convert destination into a clean initialized installation skeleton.
            for path in (
                destination / "workspace" / "projects" / "demo",
                destination / ".awoki-global" / "global" / "memories.jsonl",
                destination / ".awoki-global" / "skills" / "custom.md",
                destination / ".harness" / "artifacts" / "evidence.txt",
                destination / ".harness" / "state" / "last_project.json",
                destination / ".harness" / "notes.md",
            ):
                if path.is_dir():
                    import shutil
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            # Existing derived data requires explicit force and is then cleared.
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global", AWOKI_GLOBAL_SKILLS_DIR=destination / ".awoki-global" / "skills"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "existing derived indexes"):
                        backup.restore_backup(destination, Path(created["archive"]), reindex="none")
                    restored = backup.restore_backup(destination, Path(created["archive"]), force=True, reindex="none")
            self.assertEqual(restored["status"], "restored")
            self.assertIn("canonical", (destination / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl").read_text())
            self.assertIn("global", (destination / ".awoki-global" / "global" / "memories.jsonl").read_text())
            self.assertFalse((destination / ".harness" / "index" / "awoki_project_fts.sqlite").exists())
            self.assertFalse((destination / "data" / "qdrant" / "collection.bin").exists())
            marker = json.loads((destination / ".harness" / "state" / "layout_initialized.json").read_text())
            self.assertEqual(marker["status"], "initialized")

    def test_restore_refuses_existing_canonical_data_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            destination = make_root(base, "destination")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            # Remove derived conflicts so this test reaches canonical conflict detection.
            import shutil
            shutil.rmtree(destination / "data" / "qdrant")
            shutil.rmtree(destination / ".harness" / "index")
            (destination / ".awoki-global" / "global" / "awoki_global_fts.sqlite").unlink()
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "would overwrite existing Awoki runtime data"):
                        backup.restore_backup(destination, Path(created["archive"]), reindex="none")


    def test_source_symlink_that_escapes_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            outside = base / "outside-secret.txt"
            outside.write_text("secret", encoding="utf-8")
            link = root / "workspace" / "projects" / "demo" / "outside-link"
            link.symlink_to(outside)
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "absolute symlink is not portable|escapes archived payload root"):
                        backup.create_backup(root, mode="portable", output_dir=base / "backups")

    def test_safe_relative_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            repo = root / "workspace" / "projects" / "demo" / "repo"
            repo.mkdir()
            (repo / "target.txt").write_text("target", encoding="utf-8")
            (repo / "link.txt").symlink_to("target.txt")
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.create_backup(root, mode="portable", output_dir=base / "backups")
            with tarfile.open(result["archive"], "r:gz") as tar:
                member = tar.getmember("awoki-backup/payload/workspace/projects/demo/repo/link.txt")
                self.assertTrue(member.issym())
                self.assertEqual(member.linkname, "target.txt")

    def test_backup_rejects_repository_with_external_git_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            repo = root / "workspace" / "projects" / "demo" / "repo"
            repo.mkdir()
            external_git = base / "external.git"
            (external_git / "objects").mkdir(parents=True)
            (repo / ".git").write_text(f"gitdir: {external_git}\n", encoding="utf-8")
            with self.assertRaisesRegex(backup.BackupError, "external Git metadata"):
                backup.create_backup(root, mode="portable", output_dir=base / "backups")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is not supported")
    def test_backup_rejects_special_files_in_canonical_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            fifo = root / "workspace" / "projects" / "demo" / "runtime.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(backup.BackupError, "unsupported socket/device/FIFO"):
                backup.create_backup(root, mode="portable", output_dir=base / "backups")

    def test_backup_output_cannot_be_inside_archived_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "outside the Awoki repository"):
                        backup.create_backup(root, mode="portable", output_dir=root / "workspace" / "backups")

    def test_full_restore_blocks_vector_compatibility_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global", AWOKI_VECTOR_SIZE="768"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="full", output_dir=base / "backups")
            destination = make_root(base, "destination")
            import shutil
            shutil.rmtree(destination / "workspace" / "projects" / "demo")
            for path in (
                destination / ".harness" / "artifacts" / "evidence.txt",
                destination / ".harness" / "state" / "last_project.json",
                destination / ".harness" / "notes.md",
                destination / ".awoki-global" / "global" / "memories.jsonl",
                destination / ".awoki-global" / "skills" / "custom.md",
            ):
                if path.exists():
                    path.unlink()
            shutil.rmtree(destination / ".harness" / "index")
            shutil.rmtree(destination / "data" / "qdrant")
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global", AWOKI_VECTOR_SIZE="1024"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "AWOKI_VECTOR_SIZE differs"):
                        backup.restore_backup(destination, Path(created["archive"]), reindex="none")

    def test_portable_restore_auto_reindexes_lexically(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            destination = base / "destination"
            (destination / ".harness" / "state").mkdir(parents=True)
            init = destination / "init-awoki.sh"
            init.write_text(
                "#!/usr/bin/env bash\nset -eu\nmkdir -p \"$AWOKI_ROOT/.harness/state\" \"$AWOKI_GLOBAL_ROOT/global\"\n"
                "printf '{\\\"status\\\":\\\"initialized\\\"}\\n' > \"$AWOKI_ROOT/.harness/state/layout_initialized.json\"\n",
                encoding="utf-8",
            )
            init.chmod(0o755)
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]), \
                     mock.patch.object(backup, "_reindex_restored", return_value={"status": "indexed", "mode": "lexical"}) as reindex:
                    result = backup.restore_backup(destination, Path(created["archive"]), reindex="auto")
            self.assertEqual(result["status"], "restored")
            self.assertEqual(reindex.call_args.kwargs["mode"], "lexical")

    def test_stale_operation_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            lock = root / ".harness" / "state" / backup.LOCK_NAME
            lock.write_text("pid=99999999 created=old\n", encoding="utf-8")
            with mock.patch.object(backup, "_process_is_alive", return_value=False):
                with backup._operation_lock(root):
                    self.assertTrue(lock.exists())
                    self.assertIn(f"pid={os.getpid()}", lock.read_text(encoding="utf-8"))
            self.assertFalse(lock.exists())

    def test_operation_lock_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            outside = base / "outside.txt"
            outside.write_text("not a lock", encoding="utf-8")
            lock = root / ".harness" / "state" / backup.LOCK_NAME
            lock.symlink_to(outside)
            with self.assertRaisesRegex(backup.BackupError, "not a regular file"):
                with backup._operation_lock(root):
                    pass
            self.assertEqual(outside.read_text(encoding="utf-8"), "not a lock")

    def test_backup_discards_capture_when_service_starts_mid_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            running = [{"compose_file": "docker-compose.yml", "service": "awoki-mcp"}]
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(
                    backup,
                    "_running_compose_services",
                    side_effect=[[], running],
                ):
                    with self.assertRaisesRegex(backup.BackupError, "started during backup"):
                        backup.create_backup(root, mode="portable", output_dir=base / "backups")
            self.assertEqual(list((base / "backups").iterdir()), [])

    def test_container_state_check_fails_closed_when_compose_cannot_be_queried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            failed = mock.Mock(returncode=1, stdout="", stderr="permission denied")
            with mock.patch.object(backup.shutil, "which", return_value="/usr/bin/docker"), \
                 mock.patch.object(backup.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(backup.BackupError, "Docker Compose is unavailable"):
                    backup._running_compose_services(root)

    def test_full_backup_requires_all_services_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            running = [{"compose_file": "docker-compose.opencode.yml", "service": "awoki-opencode-ssh"}]
            with mock.patch.object(backup, "_running_compose_services", return_value=running):
                with self.assertRaisesRegex(backup.BackupError, "require complete quiescence"):
                    backup.create_backup(root, mode="full", output_dir=base / "backups", allow_live=True)

    def test_restore_refuses_any_running_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            destination = base / "destination"
            (destination / ".harness" / "state").mkdir(parents=True)
            running = [{"compose_file": "docker-compose.yml", "service": "awoki-mcp"}]
            with mock.patch.object(backup, "_running_compose_services", return_value=running):
                with self.assertRaisesRegex(backup.BackupError, "restore is never applied to live runtime data"):
                    backup.restore_backup(destination, Path(created["archive"]), reindex="none")

    def test_create_self_verifies_and_removes_failed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_root(base)
            output = base / "backups"
            with env(AWOKI_GLOBAL_ROOT=root / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]), \
                     mock.patch.object(backup, "verify_backup", side_effect=backup.BackupError("verification failed")):
                    with self.assertRaisesRegex(backup.BackupError, "verification failed"):
                        backup.create_backup(root, mode="portable", output_dir=output)
            self.assertEqual(list(output.iterdir()), [])

    def test_restore_does_not_ignore_repository_readme_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            destination = base / "destination"
            (destination / ".harness" / "state").mkdir(parents=True)
            (destination / "workspace" / "projects" / "existing" / "repo").mkdir(parents=True)
            (destination / "workspace" / "projects" / "existing" / "repo" / "README.md").write_text(
                "canonical destination repository", encoding="utf-8"
            )
            init = destination / "init-awoki.sh"
            init.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            init.chmod(0o755)
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, "existing/repo/README.md"):
                        backup.restore_backup(destination, Path(created["archive"]), reindex="none")

    def test_restore_aborts_before_apply_when_service_starts_during_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            destination = make_root(base, "destination")
            original = (
                destination / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl"
            ).read_text(encoding="utf-8")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            running = [{"compose_file": "docker-compose.yml", "service": "awoki-mcp"}]
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(
                    backup,
                    "_running_compose_services",
                    side_effect=[[], running],
                ):
                    with self.assertRaisesRegex(backup.BackupError, "started during restore staging"):
                        backup.restore_backup(
                            destination,
                            Path(created["archive"]),
                            force=True,
                            reindex="none",
                        )
            self.assertEqual(
                (destination / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl").read_text(encoding="utf-8"),
                original,
            )

    def test_force_restore_extracts_before_deleting_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            destination = make_root(base, "destination")
            original = (
                destination / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl"
            ).read_text(encoding="utf-8")
            original_qdrant = (destination / "data" / "qdrant" / "collection.bin").read_bytes()
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]), \
                     mock.patch.object(
                         backup,
                         "_extract_payload_to_staging",
                         side_effect=backup.BackupError("staging failed"),
                     ):
                    with self.assertRaisesRegex(backup.BackupError, "staging failed"):
                        backup.restore_backup(
                            destination,
                            Path(created["archive"]),
                            force=True,
                            reindex="none",
                        )
            self.assertEqual(
                (destination / "workspace" / "projects" / "demo" / "memory" / "continuity.jsonl").read_text(encoding="utf-8"),
                original,
            )
            self.assertEqual(
                (destination / "data" / "qdrant" / "collection.bin").read_bytes(),
                original_qdrant,
            )

    def test_force_restore_preserves_tracked_artifact_readmes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            with env(AWOKI_GLOBAL_ROOT=source / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")
            destination = make_root(base, "destination")
            tracked = destination / ".harness" / "artifacts" / "evidence" / "README.md"
            tracked.parent.mkdir(parents=True, exist_ok=True)
            tracked.write_text("tracked destination documentation\n", encoding="utf-8")
            with env(AWOKI_GLOBAL_ROOT=destination / ".awoki-global"):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    backup.restore_backup(
                        destination, Path(created["archive"]), force=True, reindex="none"
                    )
            self.assertEqual(tracked.read_text(encoding="utf-8"), "tracked destination documentation\n")
            self.assertEqual(
                (destination / ".harness" / "artifacts" / "evidence.txt").read_text(encoding="utf-8"),
                "evidence",
            )

    def test_external_global_and_skills_restore_to_destination_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            source_global = base / "source-global"
            source_skills = base / "source-skills"
            (source_global / "global").mkdir(parents=True)
            (source_global / "global" / "external.jsonl").write_text("external-global\n", encoding="utf-8")
            source_skills.mkdir()
            (source_skills / "external-skill.md").write_text("external-skill\n", encoding="utf-8")
            with env(AWOKI_GLOBAL_ROOT=source_global, AWOKI_GLOBAL_SKILLS_DIR=source_skills):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(source, mode="portable", output_dir=base / "backups")

            destination = base / "destination"
            (destination / ".harness" / "state").mkdir(parents=True)
            init = destination / "init-awoki.sh"
            init.write_text(
                "#!/usr/bin/env bash\nset -eu\nmkdir -p \"$AWOKI_ROOT/.harness/state\" \"$AWOKI_GLOBAL_ROOT/global\"\n"
                "printf '{\"status\":\"initialized\"}\\n' > \"$AWOKI_ROOT/.harness/state/layout_initialized.json\"\n",
                encoding="utf-8",
            )
            init.chmod(0o755)
            destination_global = base / "destination-global"
            destination_skills = base / "destination-skills"
            with env(AWOKI_GLOBAL_ROOT=destination_global, AWOKI_GLOBAL_SKILLS_DIR=destination_skills):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    result = backup.restore_backup(destination, Path(created["archive"]), reindex="none")
            self.assertEqual(
                (destination_global / "global" / "external.jsonl").read_text(encoding="utf-8"),
                "external-global\n",
            )
            self.assertEqual(
                (destination_skills / "external-skill.md").read_text(encoding="utf-8"),
                "external-skill\n",
            )
            self.assertEqual(result["additional_global_roots"], [str(destination_global.resolve())])

    def test_relative_configured_roots_are_resolved_from_awoki_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            (root / ".env").write_text(
                "AWOKI_GLOBAL_ROOT=.runtime/global\n"
                "AWOKI_GLOBAL_SKILLS_DIR=.runtime/skills\n",
                encoding="utf-8",
            )
            with env(
                AWOKI_GLOBAL_ROOT=None,
                HARNESS_GLOBAL_ROOT=None,
                AWOKI_GLOBAL_SKILLS_DIR=None,
                HARNESS_GLOBAL_SKILLS_DIR=None,
            ):
                roots = backup._configured_roots(root)
            self.assertEqual(roots["configured_global"], (root / ".runtime" / "global").resolve())
            self.assertEqual(roots["configured_skills"], (root / ".runtime" / "skills").resolve())

    def test_archived_dotenv_paths_cannot_be_masked_by_destination_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_root(base, "source")
            source_global = base / "source-absolute-global"
            source_skills = base / "source-absolute-skills"
            source_global.mkdir()
            source_skills.mkdir()
            (source / ".env").write_text(
                f"AWOKI_GLOBAL_ROOT={source_global}\n"
                f"AWOKI_GLOBAL_SKILLS_DIR={source_skills}\n",
                encoding="utf-8",
            )
            with env(AWOKI_GLOBAL_ROOT=source_global, AWOKI_GLOBAL_SKILLS_DIR=source_skills):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    created = backup.create_backup(
                        source,
                        mode="portable",
                        output_dir=base / "backups",
                        include_secrets=True,
                    )
            destination = base / "destination"
            (destination / ".harness" / "state").mkdir(parents=True)
            destination_global = base / "destination-global"
            destination_skills = base / "destination-skills"
            with env(
                AWOKI_GLOBAL_ROOT=destination_global,
                AWOKI_GLOBAL_SKILLS_DIR=destination_skills,
            ):
                with mock.patch.object(backup, "_running_compose_services", return_value=[]):
                    with self.assertRaisesRegex(backup.BackupError, r"archived \.env resolves a different destination path"):
                        backup.restore_backup(destination, Path(created["archive"]), reindex="none")

    def test_restore_target_overlap_resolves_symlinked_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "destination"
            workspace = root / "workspace"
            workspace.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(workspace, target_is_directory=True)
            rows = [{"role": "workspace"}, {"role": "global_configured"}]
            targets = {"workspace": workspace, "global_configured": alias / "nested"}
            with self.assertRaisesRegex(backup.BackupError, "targets overlap"):
                backup._validate_restore_targets(root, rows, targets)

    def test_restore_rejects_broad_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "destination"
            rows = [{"role": "global_configured"}]
            with self.assertRaisesRegex(backup.BackupError, "too broad"):
                backup._validate_restore_targets(
                    root,
                    rows,
                    {"global_configured": Path("/etc")},
                )

    def test_restore_rejects_canonical_alias_of_top_level_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "destination"
            rows = [{"role": "global_configured"}]
            with mock.patch.object(
                backup,
                "_resolved_top_level_aliases",
                return_value={Path("/private/etc")},
            ):
                with self.assertRaisesRegex(backup.BackupError, "too broad"):
                    backup._validate_restore_targets(
                        root,
                        rows,
                        {"global_configured": Path("/private/etc")},
                    )

    def test_full_restore_rejects_unproven_latest_qdrant_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            manifest = {
                "configuration": backup._nonsecret_config(root),
                "qdrant_image": {"reference": "qdrant/qdrant:latest", "image_id": "", "repo_digests": []},
            }
            destination_identity = {
                "reference": "qdrant/qdrant:latest",
                "image_id": "",
                "repo_digests": [],
            }
            with mock.patch.object(backup, "_qdrant_image_reference", return_value="qdrant/qdrant:latest"), \
                 mock.patch.object(backup, "_qdrant_image_identity", return_value=destination_identity):
                issues = backup._full_compatibility_issues(root, manifest)
            self.assertTrue(any("matching image identity could not be proven" in item for item in issues))

    def test_full_restore_compares_normalization_and_actual_embedding_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            manifest = {
                "configuration": {
                    **backup._nonsecret_config(root),
                    "AWOKI_EMBEDDING_NORMALIZE": "0",
                    "AWOKI_EMBEDDING_DEPLOYMENT_ID": "other/model@revision",
                },
                "payloads": [{"role": "qdrant", "regular_file_bytes": 1}],
                "qdrant_image": {"reference": "qdrant/qdrant@sha256:test", "image_id": "", "repo_digests": []},
            }
            with mock.patch.object(backup, "_qdrant_image_reference", return_value="qdrant/qdrant@sha256:test"), \
                 mock.patch.object(
                     backup,
                     "_qdrant_image_identity",
                     return_value={"reference": "qdrant/qdrant@sha256:test", "image_id": "", "repo_digests": []},
                 ):
                issues = backup._full_compatibility_issues(root, manifest)
            self.assertTrue(any("AWOKI_EMBEDDING_NORMALIZE differs" in item for item in issues))
            self.assertTrue(any("AWOKI_EMBEDDING_DEPLOYMENT_ID differs" in item for item in issues))

    def test_full_restore_requires_actual_embedding_identity_for_nonempty_qdrant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            config = backup._nonsecret_config(root)
            config["AWOKI_EMBEDDING_DEPLOYMENT_ID"] = ""
            manifest = {
                "configuration": config,
                "payloads": [{"role": "qdrant", "regular_file_bytes": 1}],
                "qdrant_image": {"reference": "qdrant/qdrant@sha256:test", "image_id": "", "repo_digests": []},
            }
            with env(AWOKI_EMBEDDING_DEPLOYMENT_ID=None), \
                 mock.patch.object(backup, "_qdrant_image_reference", return_value="qdrant/qdrant@sha256:test"), \
                 mock.patch.object(
                     backup,
                     "_qdrant_image_identity",
                     return_value={"reference": "qdrant/qdrant@sha256:test", "image_id": "", "repo_digests": []},
                 ):
                issues = backup._full_compatibility_issues(root, manifest)
            self.assertTrue(any("deployment identity is not configured" in item for item in issues))

    def test_lock_check_removes_stale_regular_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            lock = root / ".harness" / "state" / backup.LOCK_NAME
            lock.write_text("pid=99999999 created=old\n", encoding="utf-8")
            with mock.patch.object(backup, "_process_is_alive", return_value=False):
                result = backup._check_operation_lock(root)
            self.assertEqual(result["status"], "stale_lock_removed")
            self.assertFalse(lock.exists())

    def test_verify_rejects_file_used_as_directory_payload_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "bad-root.tar.gz"
            payloads = []
            for role in (
                "workspace", "harness_state", "harness_artifacts",
                "harness_memory", "harness_notes", "global_repo",
            ):
                count = 1 if role == "workspace" else 0
                payloads.append({
                    "role": role,
                    "archive_prefix": f"{backup.PAYLOAD_ROOT}/{role}",
                    "entry_count": count,
                    "regular_file_bytes": count,
                })
            manifest = {
                "format": backup.BACKUP_FORMAT,
                "schema_version": backup.BACKUP_SCHEMA_VERSION,
                "mode": "portable",
                "payloads": payloads,
            }
            with tarfile.open(archive, "w:gz") as tar:
                raw = json.dumps(manifest).encode()
                info = tarfile.TarInfo(backup.MANIFEST_MEMBER)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
                bad = tarfile.TarInfo(f"{backup.PAYLOAD_ROOT}/workspace")
                bad.size = 1
                tar.addfile(bad, io.BytesIO(b"x"))
            backup._write_checksum(archive, backup._sha256(archive))
            with self.assertRaisesRegex(backup.BackupError, "directory payload workspace"):
                backup.verify_backup(archive)

    def test_verify_rejects_full_only_payload_in_portable_manifest(self):
        manifest = {
            "mode": "portable",
            "payloads": [
                {
                    "role": role,
                    "archive_prefix": f"{backup.PAYLOAD_ROOT}/{role}",
                    "entry_count": 0,
                    "regular_file_bytes": 0,
                }
                for role in (
                    "workspace", "harness_state", "harness_artifacts",
                    "harness_memory", "harness_notes", "global_repo", "qdrant",
                )
            ],
        }
        with self.assertRaisesRegex(backup.BackupError, "full-only"):
            backup._validate_manifest_shape(manifest)

    def test_verify_rejects_portable_excluded_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "bad-portable-member.tar.gz"
            payloads = []
            for role in (
                "workspace", "harness_state", "harness_artifacts",
                "harness_memory", "harness_notes", "global_repo",
            ):
                count = 2 if role == "workspace" else 0
                size = 1 if role == "workspace" else 0
                payloads.append({
                    "role": role,
                    "archive_prefix": f"{backup.PAYLOAD_ROOT}/{role}",
                    "entry_count": count,
                    "regular_file_bytes": size,
                })
            manifest = {
                "format": backup.BACKUP_FORMAT,
                "schema_version": backup.BACKUP_SCHEMA_VERSION,
                "mode": "portable",
                "payloads": payloads,
            }
            with tarfile.open(archive, "w:gz") as tar:
                raw = json.dumps(manifest).encode()
                info = tarfile.TarInfo(backup.MANIFEST_MEMBER)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
                root = tarfile.TarInfo(f"{backup.PAYLOAD_ROOT}/workspace")
                root.type = tarfile.DIRTYPE
                tar.addfile(root)
                bad = tarfile.TarInfo(
                    f"{backup.PAYLOAD_ROOT}/workspace/projects/demo/index/derived.bin"
                )
                bad.size = 1
                tar.addfile(bad, io.BytesIO(b"x"))
            backup._write_checksum(archive, backup._sha256(archive))
            with self.assertRaisesRegex(backup.BackupError, "excluded by portable policy"):
                backup.verify_backup(archive)

    def test_verify_rejects_path_traversal_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            archive = base / "malicious.tar.gz"
            manifest = {
                "format": backup.BACKUP_FORMAT,
                "schema_version": backup.BACKUP_SCHEMA_VERSION,
                "mode": "portable",
                "payloads": [
                    {
                        "role": role,
                        "archive_prefix": f"{backup.PAYLOAD_ROOT}/{role}",
                        "entry_count": 0,
                        "regular_file_bytes": 0,
                    }
                    for role in (
                        "workspace", "harness_state", "harness_artifacts",
                        "harness_memory", "harness_notes", "global_repo",
                    )
                ],
            }
            with tarfile.open(archive, "w:gz") as tar:
                raw = json.dumps(manifest).encode()
                info = tarfile.TarInfo(backup.MANIFEST_MEMBER)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
                bad = tarfile.TarInfo("awoki-backup/../../escape")
                bad.size = 1
                tar.addfile(bad, io.BytesIO(b"x"))
            digest = backup._sha256(archive)
            backup._write_checksum(archive, digest)
            with self.assertRaisesRegex(backup.BackupError, "unsafe archive path"):
                backup.verify_backup(archive)


if __name__ == "__main__":
    unittest.main()
