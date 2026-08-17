from __future__ import annotations

import unittest
from unittest import mock

from code_search import languages, parser


class _Node:
    def __init__(self, node_type: str, start: int, end: int, *, fields=None, children=None):
        self.type = node_type
        self.start_byte = start
        self.end_byte = end
        self.start_point = (0, start)
        self.end_point = (0, end)
        self.has_error = False
        self.named_children = list(children or [])
        self.children = self.named_children
        self.prev_named_sibling = None
        self.parent = None
        self._fields = dict(fields or {})
        for child in self.named_children:
            child.parent = self
        for child in self._fields.values():
            if child is not None:
                child.parent = self

    def child_by_field_name(self, name: str):
        return self._fields.get(name)


class _Tree:
    def __init__(self, root):
        self.root_node = root


class _Parser:
    def __init__(self, root):
        self.root = root

    def parse(self, data: bytes):
        return _Tree(self.root)


class CallReferenceIdentityTests(unittest.TestCase):
    def test_chained_java_calls_use_callee_occurrence_not_outer_node_start(self):
        data = b"Optional.of(x).map(a).map(b);"
        first_map = data.index(b"map")
        second_map = data.index(b"map", first_map + 1)
        first_name = _Node("identifier", first_map, first_map + 3)
        second_name = _Node("identifier", second_map, second_map + 3)
        first_call = _Node(
            "method_invocation", 0, data.index(b")", first_map) + 1,
            fields={"name": first_name}, children=[first_name],
        )
        second_call = _Node(
            "method_invocation", 0, len(data) - 1,
            fields={"name": second_name}, children=[second_name],
        )
        root = _Node("program", 0, len(data), children=[first_call, second_call])
        spec = languages.BY_NAME["java"]
        with (
            mock.patch.object(parser, "load_parser", return_value=_Parser(root)),
            mock.patch.object(parser, "parser_runtime_profile", return_value={"version": "test"}),
        ):
            parsed = parser._tree_sitter_parse("D4DTokenProvider.java", data, spec, "embedding-test")
        calls = [r for r in parsed.references if r.reference_kind == "call" and r.target_name == "map"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({r.reference_id for r in calls}), 2)
        self.assertEqual([r.column for r in calls], [first_map, second_map])
        self.assertNotEqual(calls[0].source_text, calls[1].source_text)

    def test_call_anchor_prefers_rightmost_matching_identifier(self):
        data = b"client.service.map"
        client = _Node("identifier", 0, 6)
        service = _Node("identifier", 7, 14)
        name = _Node("identifier", 15, 18)
        candidate = _Node("field_access", 0, 18, children=[client, service, name])
        self.assertIs(parser._call_anchor_node(data, candidate, "map"), name)


if __name__ == "__main__":
    unittest.main()
