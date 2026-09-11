from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import check_code_index
import code_index_jobs
import project_workspace
import runtime_safety
from code_search import engine, parser, store
from code_search.models import BranchIdentity
from harness_core import HarnessPaths, project_create


class CodeIndexDiagnosticsTests(unittest.TestCase):
    def fixture(self, root):
        source = b'def target():\n    return "PRIVATE_FIXTURE_SOURCE"\n\ndef entry():\n    return target()\n'
        parsed = parser.parse_source("flow.py", source, "test-profile")
        self.assertTrue(parsed.symbols and parsed.chunks and parsed.references)
        kwargs = dict(
            file_id="fixture-file", project_id="demo",
            branch=BranchIdentity("demo:repo", "repo:repo|branch:main", "main", "fixture", False, "test"),
            rel_path="flow.py", content_hash="fixture-hash", size_bytes=len(source),
            parsed=parsed, indexed_at="2026-09-11T00:00:00Z",
        )
        db = root / "index.sqlite"
        store.replace_file(db, **kwargs)
        return db, kwargs

    def snapshot(self, db):
        with closing(sqlite3.connect(db)) as conn:
            return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("code_files", "code_symbols", "code_chunks", "code_references", "code_chunks_fts")}

    def test_duplicate_ids_report_exact_context_and_preserve_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            db, kwargs = self.fixture(Path(td))
            before = self.snapshot(db)
            for _ in range(3):
                store.replace_file(db, **kwargs)
                self.assertEqual(self.snapshot(db), before)
            for field, kind in (("symbols", "symbol"), ("chunks", "chunk")):
                with self.subTest(field=field):
                    items = getattr(kwargs["parsed"], field)
                    malformed = replace(kwargs["parsed"], **{field: items + (items[0],)})
                    with self.assertRaises(store.IndexWriteError) as caught:
                        store.replace_file(db, **{**kwargs, "parsed": malformed})
                    exc = caught.exception
                    self.assertIsInstance(exc, sqlite3.IntegrityError)
                    diagnostic = exc.index_failure
                    local_id = getattr(items[0], f"{kind}_id")
                    expected = hashlib.sha256(f"fixture-file|{kind}|{local_id}".encode()).hexdigest()
                    self.assertEqual(diagnostic["conflicting_id"], expected)
                    self.assertEqual(diagnostic["table"], "code_" + field)
                    self.assertEqual(diagnostic["path"], "flow.py")
                    self.assertEqual(diagnostic["repo_id"], "demo:repo")
                    self.assertEqual(diagnostic["existing_file_in_transaction"]["path"], "flow.py")
                    self.assertNotIn("PRIVATE_FIXTURE_SOURCE", str(exc))
                    self.assertEqual(self.snapshot(db), before)

    def test_orphan_collisions_report_all_three_tables_without_repair(self):
        for stage, table in enumerate(("code_symbols", "code_chunks", "code_references")):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as td:
                db, kwargs = self.fixture(Path(td))
                # Deliberately corrupt ONLY this disposable fixture, mimicking
                # a manual parent deletion on a connection with FK checks off.
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("DELETE FROM code_files")
                    if stage >= 1:
                        conn.execute("DELETE FROM code_symbols")
                    if stage >= 2:
                        conn.execute("DELETE FROM code_chunks")
                before = self.snapshot(db)
                with self.assertRaises(store.IndexWriteError) as caught:
                    store.replace_file(db, **kwargs)
                self.assertEqual(caught.exception.index_failure["table"], table)
                self.assertTrue(caught.exception.index_failure["conflicting_id"])
                self.assertEqual(self.snapshot(db), before)
                report = check_code_index.inspect_database(db)
                self.assertEqual(report["status"], "issues_found", report)
                self.assertGreater(report["orphan_counts"][f"{table}.file_id -> code_files.file_id"], 0)
                self.assertEqual(self.snapshot(db), before)

    def make_job(self, root):
        paths = HarnessPaths(root=root, global_root=root / "global")
        project_create("demo", paths=paths)
        repo = project_workspace.paths_for(root, "demo").project_dir / "repo"
        for name in ("aaa.py", "bbb.py", "ccc.py"):
            (repo / name).write_text('def value():\n    return "PRIVATE_FIXTURE_SOURCE"\n')
        state_path = code_index_jobs._state_path(root, "demo", "cir_diagnostic")
        code_index_jobs._write_json(state_path, {
            "job_id": "cir_diagnostic", "scope_type": "repository", "scope_ids": [""],
            "status": "queued", "force": True,
        })
        return state_path

    def test_worker_reports_failed_file_not_previous_success_and_logs_no_source(self):
        for mode in ("constraint", "parser"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                with mock.patch.dict(os.environ, {"AWOKI_GLOBAL_SKILLS_DIR": str(root / "skills"), "AWOKI_DISABLE_QDRANT": "1"}):
                    state_path = self.make_job(root)
                    real_parse = engine.parse_source

                    def parse(*args, **kwargs):
                        parsed = real_parse(*args, **kwargs)
                        if args[0] == "bbb.py":
                            if mode == "parser":
                                raise ValueError("synthetic parser failure")
                            return replace(parsed, symbols=parsed.symbols + (parsed.symbols[0],))
                        return parsed

                    output = io.StringIO()
                    with mock.patch.object(engine, "parse_source", side_effect=parse), redirect_stdout(output):
                        rc = code_index_jobs._worker(root, "demo", "cir_diagnostic")
                    state = code_index_jobs._read_json(state_path)
                    summary = code_index_jobs._job_progress_summary(state)
                    self.assertEqual(rc, 1)
                    self.assertEqual(state["status"], "failed")
                    self.assertEqual(summary["current_path"], "bbb.py")
                    self.assertEqual(summary["files_parsed"], 1)  # aaa.py succeeded; bbb.py did not.
                    self.assertEqual(summary["files_processed"], 1)
                    self.assertEqual(summary["failure"]["path"], "bbb.py")
                    self.assertEqual(summary["failure"]["phase"], "replace_file" if mode == "constraint" else "parse_source")
                    self.assertTrue(summary["failure"]["stack"])
                    self.assertIn('"path": "bbb.py"', output.getvalue())
                    self.assertNotIn("PRIVATE_FIXTURE_SOURCE", output.getvalue() + json.dumps(state))
                    if mode == "constraint":
                        self.assertEqual(summary["failure"]["table"], "code_symbols")

    def test_unknown_worker_failure_does_not_blame_previous_progress_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = self.make_job(root)

            def fail(*args, progress_callback, **kwargs):
                progress_callback({"current_path": "previous.py", "phase": "structural_index"})
                raise RuntimeError("Authorization: Bearer sk-" + "s" * 30)

            output = io.StringIO()
            with mock.patch.object(engine, "index_project_code", side_effect=fail), redirect_stdout(output):
                self.assertEqual(code_index_jobs._worker(root, "demo", "cir_diagnostic"), 1)
            state = code_index_jobs._read_json(state_path)
            self.assertEqual(code_index_jobs._job_progress_summary(state)["current_path"], "")
            self.assertNotIn("s" * 30, output.getvalue() + json.dumps(state))

    def test_checker_is_read_only_and_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missing = root / "missing" / "index.sqlite"
            self.assertEqual(check_code_index.inspect_database(missing)["status"], "missing")
            self.assertFalse(missing.parent.exists())
            db, _ = self.fixture(root)
            before = db.read_bytes()
            with mock.patch.object(store, "init_db", side_effect=AssertionError("must not initialize")), \
                 mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                report = check_code_index.inspect_database(db)
                self.assertEqual(report["status"], "ok", report)
                self.assertTrue(connect.call_args.kwargs["uri"])
                self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))
            self.assertEqual(db.read_bytes(), before)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("PRAGMA user_version=3")
            before = db.read_bytes()
            self.assertEqual(check_code_index.inspect_database(db)["status"], "unsupported_schema")
            self.assertEqual(db.read_bytes(), before)

    def test_checker_reads_live_wal_and_detects_missing_cascade(self):
        with tempfile.TemporaryDirectory() as td:
            db, _ = self.fixture(Path(td))
            with closing(sqlite3.connect(db)) as writer:
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("DELETE FROM code_files")
                writer.commit()
                self.assertTrue(Path(str(db) + "-wal").exists())
                report = check_code_index.inspect_database(db)
                self.assertEqual(report["status"], "issues_found", report)
                self.assertEqual(report["row_counts"]["code_files"], 0)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("DROP TABLE code_symbols")
                conn.execute("CREATE TABLE code_symbols(symbol_id TEXT PRIMARY KEY, file_id TEXT)")
            report = check_code_index.inspect_database(db)
            self.assertIn("code_symbols.file_id -> code_files.file_id", report["missing_cascades"])

    def test_checker_never_executes_deployed_code_and_handles_missing_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            deployed = root / ".harness" / "code_search" / "store.py"
            deployed.parent.mkdir(parents=True)
            deployed.write_text('raise RuntimeError("do not import me")\nSCHEMA_VERSION = 4\n')
            code = check_code_index.inspect_code(root)
            self.assertEqual(code[".harness/code_search/store.py"]["SCHEMA_VERSION"], 4)
            db = root / "index.sqlite"
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("PRAGMA user_version=4")
            report = check_code_index.inspect_database(db)
            self.assertEqual(report["status"], "schema_mismatch")
            self.assertIn("code_files", report["missing_columns"])

    def test_checker_timeout_is_incomplete_not_a_pass(self):
        with tempfile.TemporaryDirectory() as td:
            db, _ = self.fixture(Path(td))
            # Ensure at least one statement crosses the progress-handler VM
            # interval, independently of the SQLite version's tiny-query plan.
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.executemany(
                    "INSERT INTO code_chunks_fts(chunk_id, text) VALUES(?, ?)",
                    [(f"extra-{index}", "fixture") for index in range(2000)],
                )
            with mock.patch.object(check_code_index.time, "monotonic", side_effect=[0] + [100] * 1000):
                report = check_code_index.inspect_database(db, timeout_seconds=1)
            self.assertEqual(report["status"], "incomplete", report)
            self.assertEqual(report["sqlite_errorname"], "SQLITE_INTERRUPT")

    def test_stdin_checker_runs_against_separate_installation_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root / "workspace" / "projects" / "demo" / "index" / "sqlite"
            db, _ = self.fixture(directory)
            db.rename(directory / "awoki_code.sqlite")
            for relative in check_code_index.CODE_FILES:
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text('raise RuntimeError("do not execute deployed code")\nSCHEMA_VERSION = 4\n')
            script = Path(check_code_index.__file__).read_text()
            for project, expected in (("demo", 0), ("missing", 1), ("../outside", 2)):
                with self.subTest(project=project):
                    result = subprocess.run(
                        [sys.executable, "-B", "-", "--root", str(root), "--project", project],
                        input=script, capture_output=True, text=True, cwd=root,
                        env=runtime_safety.credential_free_environment(), timeout=30,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
                    if expected != 2:
                        report = json.loads(result.stdout)
                        self.assertEqual(report["root"], str(root.resolve()))
                        self.assertEqual(report["database"]["status"], "ok" if expected == 0 else "missing")
                        self.assertNotIn("PRIVATE_FIXTURE_SOURCE", result.stdout)


if __name__ == "__main__":
    unittest.main()
