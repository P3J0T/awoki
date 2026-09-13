import asyncio
import importlib.util
import unittest
from unittest import mock


@unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK required for wire schema check")
class CaptureSchemaTests(unittest.TestCase):
    def test_typed_item_schema_and_preserved_alias(self):
        import server
        from pydantic import ValidationError
        schema = server.CaptureItem.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("evidence_refs", schema["properties"])
        self.assertIn("based_on", schema["properties"])
        self.assertEqual(schema["required"], ["summary"])
        with self.assertRaises(ValidationError):
            server.CaptureItem(summary="Note", evidence_refs=["ev_example"], sensitivity="project")
        with self.assertRaises(ValidationError):
            server.CaptureItem(summary="x" * 601)
        with self.assertRaises(ValidationError):
            server.CaptureItem(summary="Note", evidence_refs=["ev_example"] * 13)
        with self.assertRaises(ValidationError):
            server.CaptureItem(summary="Note", based_on=["cont_example"] * 4)
        item = server.CaptureItem(summary="Note", evidence_refs=["ev_example"])
        with mock.patch.object(server, "core_project_capture", return_value={}) as capture:
            server.project_capture(items=[item])
        self.assertEqual(capture.call_args.kwargs["items"], [{"summary": "Note", "evidence_refs": ["ev_example"]}])
        tools = asyncio.run(server.mcp.list_tools())
        tool = next(t for t in tools if t.name == "project_capture")
        self.assertIn("CaptureItem", str(tool.inputSchema))
        wire = str(tool.inputSchema)
        self.assertNotIn("$ref", wire)
        self.assertNotIn("maxLength", wire)
        self.assertNotIn("maxItems", wire)
        self.assertIn("evidence_refs", wire)
        self.assertIn("based_on", wire)
        self.assertIn('INSIDE EACH', tool.description)
        self.assertIn('lightweight investigation checkpoints', tool.description)
        task_tool = next(t for t in tools if t.name == 'project_task_checkpoint')
        self.assertIn("NOT for 'checkpoint this investigation'", task_tool.description)
        self.assertIn("project_capture(kind='reflection')", task_tool.description)
        # Wire compatibility must not remove actual transport validation.
        try:
            result = asyncio.run(server.mcp.call_tool("project_capture", {"items": [{"summary": "x" * 601}]}))
        except Exception as exc:
            result = exc
        self.assertIn("600", str(result))
        window = next(t for t in tools if t.name == "code_source_window")
        self.assertIn("evidence_ref", window.inputSchema["properties"])
        self.assertNotIn("path", window.inputSchema.get("required", []))
