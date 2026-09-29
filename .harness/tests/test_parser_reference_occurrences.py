from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import project_workspace
from code_search import engine, languages, parser, store
from code_search.models import BranchIdentity
from harness_core import HarnessPaths, code_index_verify, project_create


def go_chain_source(*, repeated: bool = False, multiline: bool = False) -> bytes:
    # The callee of each outer call exceeds the 500-character display-hint
    # limit. Distinct occurrences must not acquire the same truncated identity.
    receiver = 'client.X("' + "payload_" * 70 + '")'
    tail = ".X().X()" if repeated else ".Y().Z()"
    if multiline:
        tail = tail.replace(".", ".\n        ")
    return ("package main\n\nfunc TestChain() {\n    " + receiver + tail + "\n}\n").encode()


class ParserReferenceOccurrenceTests(unittest.TestCase):
    def require_tree_sitter(self) -> None:
        if not languages.parser_runtime_profile()["available"]:
            self.skipTest("tree-sitter-language-pack is not installed in this test environment")

    def assert_distinct_references(self, parsed) -> None:
        self.assertEqual(
            len({reference.reference_id for reference in parsed.references}),
            len(parsed.references),
            "different syntax occurrences must retain different reference identities",
        )

    def test_go_long_receiver_retains_each_callee_name_and_byte_location(self):
        self.require_tree_sitter()
        for repeated, multiline in ((False, False), (True, False), (False, True)):
            with self.subTest(repeated=repeated, multiline=multiline):
                source = go_chain_source(repeated=repeated, multiline=multiline)
                parsed = parser.parse_source("cmd/server/main_test.go", source, "local")
                self.assertEqual(parsed.parse_mode, "tree_sitter", parsed.diagnostics)
                self.assertEqual(parsed.parse_status, "ok", parsed.diagnostics)
                calls = [ref for ref in parsed.references if ref.reference_kind == "call"]
                self.assertEqual(len(calls), 3)
                self.assert_distinct_references(parsed)
                expected_names = ["X", "X", "X"] if repeated else ["X", "Y", "Z"]
                observed = sorted(calls, key=lambda ref: (ref.line, ref.column))
                self.assertEqual([ref.target_name for ref in observed], expected_names)
                cursor = 0
                for ref, name in zip(observed, expected_names):
                    offset = source.index(name.encode() + b"(", cursor)
                    self.assertEqual(ref.line, source[:offset].count(b"\n") + 1)
                    self.assertEqual(ref.column, offset - source.rfind(b"\n", 0, offset) - 1)
                    self.assertLessEqual(len(ref.target_qualified_hint), 500)
                    cursor = offset + len(name)
                second_parse = parser.parse_source("cmd/server/main_test.go", source, "local")
                self.assertEqual(parsed.references, second_parse.references)

    def test_tree_sitter_shared_callee_anchor_and_bounded_hint_keep_distinct_calls(self):
        self.require_tree_sitter()
        # Calls of returned functions share the initial identifier's location.
        # Once the display hint is bounded, even its text cannot distinguish
        # the outer occurrences; their complete syntax spans must do so.
        call_count = 255
        source = ("package main\nfunc Test() { factory" + "()" * call_count + " }\n").encode()
        parsed = parser.parse_source("returned_functions_test.go", source, "local")
        self.assertEqual(parsed.parse_mode, "tree_sitter", parsed.diagnostics)
        self.assertEqual(parsed.parse_status, "ok", parsed.diagnostics)
        calls = [ref for ref in parsed.references if ref.reference_kind == "call"]
        self.assertEqual(len(calls), call_count)
        self.assertEqual({(ref.line, ref.column) for ref in calls}, {(2, 14)})
        self.assertLess(len({ref.target_qualified_hint for ref in calls}), call_count)
        self.assertEqual(len({ref.source_text for ref in calls}), call_count)
        self.assert_distinct_references(parsed)
        unique, duplicates = store._deduplicate_references(parsed.references)
        self.assertEqual(len(unique), call_count)
        self.assertEqual(duplicates, 0)

    def test_go_long_chains_index_as_current_without_losing_occurrences(self):
        self.require_tree_sitter()
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
            os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False
        ):
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            project_workspace.enable_code_index(root, "demo")
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo"
            fixtures = {
                "cmd/server/main_test.go": go_chain_source(),
                "internal/adapters/tokens_adapter/authz_test.go": go_chain_source(repeated=True),
            }
            for rel_path, source in fixtures.items():
                target = repo / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source)

            first = engine.index_project_code(paths, "demo", include_qdrant=False, force=True)
            self.assertEqual(first["status"], "indexed", first)
            self.assertEqual(first["reference_integrity"]["identity_conflicts"], 0)
            self.assertEqual(first["reference_integrity"]["duplicate_references_deduplicated"], 0)
            verified = code_index_verify(name="demo", include_qdrant=False, paths=paths)
            self.assertTrue(verified["freshness"]["lexical_current"], verified)

            db = store.db_path(pp.project_dir)
            with closing(sqlite3.connect(db)) as conn:
                before = conn.execute(
                    "SELECT r.reference_id, f.path, r.target_name, r.line, r.column_no, r.source_text "
                    "FROM code_references r JOIN code_files f ON f.file_id=r.file_id "
                    "WHERE r.reference_kind='call' ORDER BY f.path, r.line, r.column_no"
                ).fetchall()
            self.assertEqual(len(before), 6)
            self.assertEqual(len({row[0] for row in before}), 6)
            for rel_path in fixtures:
                self.assertEqual(sum(row[1] == rel_path for row in before), 3)

            repeated = engine.index_project_code(paths, "demo", include_qdrant=False, force=True)
            self.assertEqual(repeated["status"], "indexed", repeated)
            with closing(sqlite3.connect(db)) as conn:
                after = conn.execute(
                    "SELECT r.reference_id, f.path, r.target_name, r.line, r.column_no, r.source_text "
                    "FROM code_references r JOIN code_files f ON f.file_id=r.file_id "
                    "WHERE r.reference_kind='call' ORDER BY f.path, r.line, r.column_no"
                ).fetchall()
            self.assertEqual(before, after)

    def test_python_ast_fallback_persists_nested_calls_with_shared_start(self):
        source = b"def test():\n    return factory()()()\n"
        with mock.patch.object(parser, "load_parser", side_effect=ModuleNotFoundError("test fallback")):
            parsed = parser.parse_source("nested.py", source, "local")
            second_parse = parser.parse_source("nested.py", source, "local")
        self.assertEqual(parsed.parse_mode, "python_ast_fallback")
        self.assertEqual(parsed.parse_status, "ok")
        calls = [ref for ref in parsed.references if ref.reference_kind == "call"]
        self.assertEqual({ref.source_text for ref in calls}, {"factory()", "factory()()", "factory()()()"})
        self.assertEqual(len(calls), 3)
        self.assertEqual({(ref.line, ref.column) for ref in calls}, {(2, 11)})
        self.assert_distinct_references(parsed)
        self.assertEqual(parsed.references, second_parse.references)

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "code.sqlite"
            branch = BranchIdentity(
                repo_id="demo:repo", branch_key="working-tree:test", branch_name="test",
                commit_sha="", dirty=True, source="test",
            )
            store.replace_file(
                db, file_id="nested-file", project_id="demo", branch=branch,
                rel_path="nested.py", content_hash=hashlib.sha256(source).hexdigest(),
                size_bytes=len(source), parsed=parsed, indexed_at="2026-09-29T00:00:00Z",
            )
            with closing(sqlite3.connect(db)) as conn:
                stored = conn.execute(
                    "SELECT source_text, line, column_no FROM code_references "
                    "WHERE reference_kind='call'"
                ).fetchall()
            self.assertEqual(
                set(stored), {(ref.source_text, ref.line, ref.column) for ref in calls}
            )
            self.assertEqual(len(stored), 3)


if __name__ == "__main__":
    unittest.main()
