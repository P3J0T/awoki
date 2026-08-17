from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ROOT / ".opencode" / "commands"

EXPECTED_COMMANDS = {
    "backup",
    "burp",
    "burp-intruder",
    "burp-repeater",
    "burp-send",
    "burp-status",
    "burp-validate",
    "callees",
    "callers",
    "code-across",
    "code-index-status",
    "code-path",
    "code-validate-claim",
    "codebase",
    "definition",
    "demote-memory",
    "explore",
    "harness-boot",
    "lavish",
    "project",
    "project-status",
    "recall",
    "reliability-check",
    "retrieval-status",
    "review-promotions",
    "ship-check",
    "verify",
}

REMOVED_ALIASES = {
    "project-create",
    "project-resume",
    "project-handoff",
    "project-index",
    "index-memory",
    "code-peek",
    "code-context",
    "code-full",
    "code-eval",
    "code_validate_claim",
    "burp-find-request",
    "burp-host-report",
    "burp-pull-history",
    "burp-request-to-repeater",
    "burp-request-to-intruder",
    "burp-send-request",
    "burp-tools",
    "burp-task-status",
    "save-finding",
}


class CommandSurfaceTests(unittest.TestCase):
    def test_command_surface_is_exact_and_bounded(self) -> None:
        actual = {path.stem for path in COMMANDS.glob("*.md")}
        self.assertEqual(actual, EXPECTED_COMMANDS)
        self.assertLessEqual(len(actual), 30)
        self.assertTrue(actual.isdisjoint(REMOVED_ALIASES))

    def test_every_command_has_frontmatter_description(self) -> None:
        for path in sorted(COMMANDS.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), path.name)
            self.assertIn("\ndescription:", text.split("---", 2)[1], path.name)

    def test_natural_language_front_doors_and_side_effect_boundaries(self) -> None:
        project = (COMMANDS / "project.md").read_text(encoding="utf-8")
        codebase = (COMMANDS / "codebase.md").read_text(encoding="utf-8")
        burp = (COMMANDS / "burp.md").read_text(encoding="utf-8")
        intruder = (COMMANDS / "burp-intruder.md").read_text(encoding="utf-8")
        send = (COMMANDS / "burp-send.md").read_text(encoding="utf-8")

        self.assertIn("interpret `$ARGUMENTS`", project)
        self.assertIn("include_qdrant", project)
        self.assertIn("repository-readiness", project)
        self.assertIn('kind="observation"', project)
        self.assertIn('`peek`', codebase)
        self.assertIn('`context`', codebase)
        self.assertIn('`full`', codebase)
        self.assertIn("code_flow_graph", codebase)
        self.assertIn("code_source_window", codebase)
        self.assertIn("code_text_search", codebase)
        self.assertIn("SOURCE-CONFIRMED", codebase)
        self.assertIn("code-search-fallback", codebase)
        self.assertIn("Do not start broad repository discovery with OpenCode `Grep`", codebase)
        self.assertIn("direct PortSwigger Burp MCP", burp)
        self.assertIn("explicit user intent", burp)
        self.assertIn("does not authorize starting an Intruder attack", intruder)
        self.assertIn("one network send", send)
        self.assertIn("Do not repeat a failed send automatically", send)

    def test_code_validation_command_orchestrates_broad_requests_without_weakening_atomic_proof(self) -> None:
        validate = (COMMANDS / "code-validate-claim.md").read_text(encoding="utf-8")
        self.assertIn("does not have to already match the strict MCP claim", validate)
        self.assertIn("grammar.", validate)
        self.assertIn("code_index_status", validate)
        self.assertIn("freshness.lexical_current", validate)
        self.assertIn("project_refresh(include_code=true, include_qdrant=false)", validate)
        self.assertIn("decomposing broad questions", validate)
        self.assertIn("Do not pass the original vague request straight", validate)
        self.assertIn("code_validate_claim", validate)
        self.assertIn("Semantic/FTS results are navigation, never proof", validate)
        self.assertIn("refresh_index=false", validate)
        self.assertIn("overall result inconclusive", validate)

    def test_manifest_matches_command_contract(self) -> None:
        manifest = json.loads((ROOT / ".harness" / "manifest.json").read_text(encoding="utf-8"))
        surface = manifest["command_surface"]
        declared = set()
        for key in ("primary", "code_precision", "burp_side_effects", "diagnostics", "reliability", "specialized"):
            declared.update(surface[key])
        self.assertEqual(declared, EXPECTED_COMMANDS)
        self.assertEqual(surface["authoritative_doc"], "docs/COMMANDS.md")
        self.assertEqual(surface["philosophy"], "natural_language_first_minimal_slash_surface")
        self.assertIn("code_flow_graph", surface["internal_only_examples"])
        self.assertIn("code_source_window", surface["internal_only_examples"])

    def test_distribution_has_no_operator_wireguard_endpoint(self) -> None:
        paths = [ROOT / ".env.example", ROOT / "README.md", ROOT / "HARNESS.md", ROOT / "AGENTS.md"]
        paths.extend(sorted((ROOT / "docs").glob("*.md")))
        paths.extend(sorted((ROOT / ".opencode" / "commands").glob("*.md")))
        paths.extend(sorted((ROOT / ".opencode" / "skills").glob("*/SKILL.md")))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertNotIn("10.7.0.2", combined)
        self.assertNotIn("WireGuard", combined)
        self.assertNotIn("wireguard", combined)
        dotenv = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("AWOKI_EMBEDDING_BASE_URL=\n", dotenv)
        self.assertIn("embedding.example.invalid", dotenv)

    def test_opencode_always_loads_command_contract(self) -> None:
        for name in ("opencode.jsonc", "opencode.container.jsonc"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('"docs/COMMANDS.md"', text, name)

    def test_command_documentation_lists_replacements(self) -> None:
        docs = (ROOT / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
        self.assertIn("/project", docs)
        self.assertIn("/project prime oathkeeper for full retrieval", docs)
        self.assertIn("repository-readiness", docs)
        self.assertIn("/codebase", docs)
        self.assertIn("/burp", docs)
        self.assertIn("/burp-send", docs)
        self.assertIn("Removed redundant aliases", docs)
        self.assertIn("/project-create NAME", docs)
        self.assertIn("/code_validate_claim", docs)
        self.assertIn("save_finding", docs)
        self.assertNotIn("/save-finding <", docs)


if __name__ == "__main__":
    unittest.main()
