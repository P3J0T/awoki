from __future__ import annotations

import sqlite3
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

import check_code_index
import project_workspace
import runtime_safety
from harness_core import HarnessPaths, project_create
from code_search import engine, parser, store
from code_search.models import BranchIdentity


class CodeIndexIntegrityTests(unittest.TestCase):
    def payload(self, source=b"def value():\n    return 1\n", path="value.py"):
        return dict(
            file_id="fixture-file", project_id="demo",
            branch=BranchIdentity("demo:repo", "repo:repo|branch:main", "main", "fixture", False, "test"),
            rel_path=path, content_hash="fixture-hash", size_bytes=len(source),
            parsed=parser.parse_source(path, source, "fixture"), indexed_at="2026-09-11T00:00:00Z",
        )

    def test_same_line_declarations_have_distinct_deterministic_ids(self):
        fixtures = {
            "repeat.js": b"{ function same() { return 1; } } { function same() { return 1; } }",
            "methods.ts": b"class A { same() { return 1; } same() { return 1; } }",
        }
        for path, source in fixtures.items():
            with self.subTest(path=path):
                parsed = parser.parse_source(path, source, "fixture")
                if parsed.parse_mode != "tree_sitter":
                    self.skipTest("real JS/TS grammar unavailable; exercised in Linux parser tests")
                repeated = [symbol for symbol in parsed.symbols if symbol.name == "same"]
                self.assertEqual(len(repeated), 2)
                self.assertEqual(repeated[0].start_line, repeated[1].start_line)
                self.assertNotEqual(repeated[0].start_byte, repeated[1].start_byte)
                for field, identity in (("symbols", "symbol_id"), ("chunks", "chunk_id"), ("references", "reference_id")):
                    ids = [getattr(item, identity) for item in getattr(parsed, field)]
                    self.assertEqual(len(ids), len(set(ids)), (field, path))
                self.assertEqual(parsed, parser.parse_source(path, source, "fixture"))
                with tempfile.TemporaryDirectory() as td:
                    db = Path(td) / "index.sqlite"
                    payload = self.payload(source, path)
                    for _ in range(3):
                        store.replace_file(db, **payload)
                    with closing(sqlite3.connect(db)) as conn:
                        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM code_symbols").fetchone()[0], len(parsed.symbols))

    def test_concurrent_file_replacement_reads_inside_write_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "index.sqlite"
            store.init_db(db)
            barrier = threading.Barrier(2)
            real_connect = store._connect

            class ConnectionProxy:
                def __init__(self):
                    self.connection = real_connect(db)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def __enter__(self):
                    self.connection.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.connection.__exit__(*args)

                def execute(self, sql, *args):
                    cursor = self.connection.execute(sql, *args)
                    if sql.startswith("SELECT file_id FROM code_files WHERE project_id=") and not self.connection.in_transaction:
                        # Reproduce the old check-then-insert race deterministically.
                        # Fixed code must hold a write transaction BEFORE this read.
                        cursor.fetchall()
                        barrier.wait(timeout=5)
                    return cursor

            payload = self.payload()
            with mock.patch.object(store, "init_db"), \
                 mock.patch.object(store, "_connect", side_effect=lambda _: ConnectionProxy()), \
                 ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(store.replace_file, db, **payload) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM code_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_connection_refuses_silently_disabled_foreign_keys(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "index.sqlite"
            connection = sqlite3.connect(db)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN")  # SQLite ignores FK toggles inside a transaction.
            with mock.patch.object(store.sqlite3, "connect", return_value=connection):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "foreign-key enforcement"):
                    store._connect(db)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_current_extraction_profile_invalidates_v4_and_reparses(self):
        profile = engine.parser_runtime_profile()
        self.assertEqual(profile["extraction_profile"], "awoki-symbol-extraction-v5")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            project = project_workspace.paths_for(root, "demo").project_dir
            (project / "repo" / "value.py").write_text("def value():\n    return 1\n")
            with mock.patch.object(engine, "parser_runtime_profile", return_value={
                **profile, "extraction_profile": "awoki-symbol-extraction-v4",
            }):
                first = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(first["status"], "indexed", first)
            db = store.db_path(project)
            with closing(sqlite3.connect(db)) as conn:
                before_keys = conn.execute("SELECT embedding_key FROM code_chunks ORDER BY embedding_key").fetchall()
            with mock.patch.object(engine, "parse_source", wraps=engine.parse_source) as parse:
                refreshed = engine.index_project_code(paths, "demo", include_qdrant=False)
            self.assertEqual(refreshed["status"], "indexed", refreshed)
            self.assertGreater(parse.call_count, 0)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT embedding_key FROM code_chunks ORDER BY embedding_key").fetchall(), before_keys)
            self.assertTrue(engine.index_status(paths, "demo", deep_verify=True, verify_qdrant=False)["freshness"]["lexical_current"])

    def test_five_repositories_with_identical_same_line_code_reindex_without_collisions(self):
        source = b"{ function same() { return 1; } } { function same() { return 1; } }"
        if parser.parse_source("repeat.js", source, "fixture").parse_mode != "tree_sitter":
            self.skipTest("real JavaScript grammar unavailable; exercised in Linux parser tests")
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            project = project_workspace.paths_for(root, "demo").project_dir
            env = {
                **runtime_safety.credential_free_environment(),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Awoki Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Awoki Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
            for index in range(5):
                repo = project / "repo" / f"repo-{index}"
                repo.mkdir(parents=True)
                (repo / "repeat.js").write_bytes(source)
                for command in (("init", "-b", "main"), ("add", "."), ("commit", "-m", "fixture")):
                    subprocess.run(["git", "-C", str(repo), *command], env=env,
                                   check=True, capture_output=True, timeout=10)
                project_workspace.project_repo_add(root, "demo", f"repo-{index}", f"repo/repo-{index}", default=index == 0)
            for iteration in range(3):
                for index in range(5):
                    indexed = engine.index_project_code(paths, "demo", repo=f"repo-{index}",
                                                        include_qdrant=False, force=iteration > 0)
                    self.assertEqual(indexed["status"], "indexed", indexed)
                    self.assertEqual((project / "repo" / f"repo-{index}" / "repeat.js").read_bytes(), source)
            db = store.db_path(project)
            self.assertEqual(check_code_index.inspect_database(db)["status"], "ok")
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(DISTINCT source_id) FROM code_files").fetchone()[0], 5)
                # Occurrence IDs remain separate; identical bodies still share embedding keys.
                chunks, vectors = conn.execute("SELECT COUNT(*), COUNT(DISTINCT embedding_key) FROM code_chunks").fetchone()
                self.assertGreater(chunks, vectors)


if __name__ == "__main__":
    unittest.main()
