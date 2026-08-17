from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    grammar_name: str
    extensions: tuple[str, ...]
    definition_types: tuple[str, ...]
    class_types: tuple[str, ...]
    call_types: tuple[str, ...]
    import_types: tuple[str, ...]
    branch_types: tuple[str, ...]
    return_types: tuple[str, ...]
    raise_types: tuple[str, ...]


_SPECS = (
    LanguageSpec(
        "python", "python", (".py", ".pyi"),
        ("function_definition",), ("class_definition",), ("call",),
        ("import_statement", "import_from_statement"),
        ("if_statement", "elif_clause", "while_statement", "for_statement", "match_statement"),
        ("return_statement",), ("raise_statement",),
    ),
    LanguageSpec(
        "javascript", "javascript", (".js", ".jsx", ".mjs", ".cjs"),
        ("function_declaration", "function_expression", "arrow_function", "method_definition"),
        ("class_declaration", "class"), ("call_expression", "new_expression"),
        ("import_statement",),
        ("if_statement", "while_statement", "for_statement", "switch_statement", "ternary_expression"),
        ("return_statement",), ("throw_statement",),
    ),
    LanguageSpec(
        "typescript", "typescript", (".ts",),
        ("function_declaration", "function_expression", "arrow_function", "method_definition"),
        ("class_declaration", "interface_declaration", "type_alias_declaration"),
        ("call_expression", "new_expression"), ("import_statement",),
        ("if_statement", "while_statement", "for_statement", "switch_statement", "ternary_expression"),
        ("return_statement",), ("throw_statement",),
    ),
    LanguageSpec(
        "tsx", "tsx", (".tsx",),
        ("function_declaration", "function_expression", "arrow_function", "method_definition"),
        ("class_declaration", "interface_declaration", "type_alias_declaration"),
        ("call_expression", "new_expression"), ("import_statement",),
        ("if_statement", "while_statement", "for_statement", "switch_statement", "ternary_expression"),
        ("return_statement",), ("throw_statement",),
    ),
    LanguageSpec(
        "go", "go", (".go",),
        ("function_declaration", "method_declaration"), ("type_declaration",),
        ("call_expression",), ("import_declaration",),
        ("if_statement", "for_statement", "expression_switch_statement", "type_switch_statement", "select_statement"),
        ("return_statement",), ("panic_expression",),
    ),
    LanguageSpec(
        "rust", "rust", (".rs",),
        ("function_item",), ("struct_item", "enum_item", "trait_item", "impl_item"),
        ("call_expression", "macro_invocation"), ("use_declaration",),
        ("if_expression", "while_expression", "for_expression", "match_expression", "loop_expression"),
        ("return_expression",), ("macro_invocation",),
    ),
    LanguageSpec(
        "java", "java", (".java",),
        ("method_declaration", "constructor_declaration"),
        ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"),
        ("method_invocation", "object_creation_expression"), ("import_declaration",),
        ("if_statement", "while_statement", "for_statement", "enhanced_for_statement", "switch_expression", "switch_statement"),
        ("return_statement",), ("throw_statement",),
    ),
    LanguageSpec(
        "c", "c", (".c", ".h"),
        ("function_definition",), ("struct_specifier", "enum_specifier", "union_specifier"),
        ("call_expression",), ("preproc_include",),
        ("if_statement", "while_statement", "for_statement", "switch_statement"),
        ("return_statement",), (),
    ),
    LanguageSpec(
        "cpp", "cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
        ("function_definition",), ("class_specifier", "struct_specifier", "enum_specifier", "namespace_definition"),
        ("call_expression",), ("preproc_include",),
        ("if_statement", "while_statement", "for_statement", "switch_statement", "try_statement"),
        ("return_statement",), ("throw_expression",),
    ),
    LanguageSpec(
        "csharp", "csharp", (".cs",),
        ("method_declaration", "constructor_declaration", "local_function_statement"),
        ("class_declaration", "interface_declaration", "struct_declaration", "record_declaration", "enum_declaration"),
        ("invocation_expression", "object_creation_expression"), ("using_directive",),
        ("if_statement", "while_statement", "for_statement", "foreach_statement", "switch_statement", "switch_expression"),
        ("return_statement",), ("throw_statement", "throw_expression"),
    ),
    LanguageSpec(
        "bash", "bash", (".sh", ".bash"),
        ("function_definition",), (), ("command",), (),
        ("if_statement", "while_statement", "for_statement", "case_statement"),
        ("return_statement",), (),
    ),
    LanguageSpec(
        "ruby", "ruby", (".rb",),
        ("method", "singleton_method"), ("class", "module"), ("call",), ("call",),
        ("if", "unless", "while", "until", "case"), ("return",), ("raise",),
    ),
    LanguageSpec(
        "php", "php", (".php",),
        ("function_definition", "method_declaration"),
        ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"),
        ("function_call_expression", "member_call_expression", "scoped_call_expression", "object_creation_expression"),
        ("namespace_use_declaration",),
        ("if_statement", "while_statement", "for_statement", "foreach_statement", "switch_statement", "match_expression"),
        ("return_statement",), ("throw_expression",),
    ),
    LanguageSpec(
        "sql", "sql", (".sql",),
        ("create_function", "create_procedure", "create_view"), (), ("function_call",), (),
        ("case", "if"), ("return_statement",), ("raise_statement",),
    ),
    LanguageSpec(
        "smali", "smali", (".smali",),
        (), (), (), (), (), (), (),
    ),
)

BY_EXTENSION = {extension: spec for spec in _SPECS for extension in spec.extensions}
BY_NAME = {spec.name: spec for spec in _SPECS}


def detect_language(path: Path) -> LanguageSpec | None:
    return BY_EXTENSION.get(path.suffix.lower())


def parser_runtime_profile() -> dict[str, Any]:
    distributions = (
        "tree-sitter-language-pack",
        "tree-sitter",
        "tree-sitter-c-sharp",
        "tree-sitter-embedded-template",
        "tree-sitter-yaml",
    )
    dependency_versions: dict[str, str] = {}
    for distribution in distributions:
        try:
            dependency_versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependency_versions[distribution] = "unavailable"

    version = dependency_versions["tree-sitter-language-pack"]
    available = version != "unavailable"
    language_abi: dict[str, int | str] = {
        "maximum": "unavailable",
        "minimum": "unavailable",
    }
    try:
        import tree_sitter

        language_abi = {
            "maximum": int(tree_sitter.LANGUAGE_VERSION),
            "minimum": int(tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION),
        }
    except (ImportError, AttributeError, TypeError, ValueError):
        pass

    return {
        "provider": "tree-sitter-language-pack",
        "version": version,
        "available": available,
        "dependency_versions": dependency_versions,
        "language_abi": language_abi,
        "grammar_profile": "awoki-curated-v1",
        "extraction_profile": "awoki-symbol-extraction-v4",
        "languages": sorted(BY_NAME),
        "deterministic_builtin_parsers": ["smali"],
        "runtime_downloads": False,
    }


def load_parser(spec: LanguageSpec):
    """Load a bundled parser without downloading grammars at runtime.

    Awoki pins the tree-sitter-language-pack 0.10.0 compatibility matrix.
    That release keeps pre-built grammars in its wheels and aligns the Python
    runtime with language ABI 15. Runtime grammar downloads remain disabled.
    """
    from tree_sitter_language_pack import get_parser

    return get_parser(spec.grammar_name)

_PARSER_SMOKE_FIXTURES: dict[str, bytes] = {
    "python": b"def awoki_probe():\n    return True\n",
    "javascript": b"class AwokiProbe { run() { return true; } }\n",
    "typescript": b"interface AwokiProbe { run(): boolean; }\n",
    "tsx": b"const AwokiProbe = () => <div />;\n",
    "go": b"package probe\ntype AwokiProbe interface { Run() bool }\n",
    "rust": b"trait AwokiProbe { fn run(&self) -> bool; }\n",
    "java": b"class AwokiProbe { boolean run() { return true; } }\n",
    "c": b"int awoki_probe(void) { return 1; }\n",
    "cpp": b"bool awoki_probe() { return true; }\n",
    "csharp": b"class AwokiProbe { bool Run() { return true; } }\n",
    "bash": b"awoki_probe() { return 0; }\n",
    "ruby": b"def awoki_probe\n  true\nend\n",
    "php": b"<?php function awoki_probe(): bool { return true; }\n",
    "sql": b"SELECT 1;\n",
    "smali": b".class public LA;\n.method public a()V\n    return-void\n.end method\n",
}

_PARSER_SMOKE_EXPECTED_SYMBOLS: dict[str, str] = {
    "javascript": "AwokiProbe",
    "typescript": "AwokiProbe",
    "go": "AwokiProbe",
    "rust": "AwokiProbe",
    "java": "AwokiProbe",
    "csharp": "AwokiProbe",
}


def supported_specs() -> tuple[LanguageSpec, ...]:
    return _SPECS


def validate_parser_runtime() -> dict[str, Any]:
    """Load and parse with every curated grammar without network activity."""
    profile = parser_runtime_profile()
    failures: list[dict[str, str]] = []
    validated: list[str] = []
    if not profile["available"]:
        return {**profile, "status": "unavailable", "validated": [], "failures": [{"language": "*", "reason": "package is not installed"}]}
    extraction: dict[str, dict[str, int]] = {}
    for spec in _SPECS:
        try:
            fixture = _PARSER_SMOKE_FIXTURES[spec.name]
            # Exercise Awoki's actual structural extraction, not only the
            # third-party parser constructor. This catches grammar node-name or
            # field changes during the image build where the bundled package is
            # available.
            from .parser import parse_source

            extension = spec.extensions[0]
            parsed = parse_source(f"awoki_probe{extension}", fixture, "parser-smoke-profile")
            if spec.name == "smali":
                if parsed.parse_mode != "smali_structural":
                    raise RuntimeError(f"Awoki Smali extraction degraded to {parsed.parse_mode}: {parsed.diagnostics}")
            else:
                parser = load_parser(spec)
                tree = parser.parse(fixture)
                root = tree.root_node
                if getattr(root, "has_error", False):
                    raise RuntimeError("smoke fixture produced a parse error")
            if spec.name != "smali" and parsed.parse_mode != "tree_sitter":
                raise RuntimeError(f"Awoki extraction degraded to {parsed.parse_mode}: {parsed.diagnostics}")
            if not parsed.chunks:
                raise RuntimeError("Awoki extraction produced no chunks")
            if spec.name != "sql" and not parsed.symbols:
                raise RuntimeError("Awoki extraction produced no symbols")
            expected_symbol = _PARSER_SMOKE_EXPECTED_SYMBOLS.get(spec.name)
            if expected_symbol and expected_symbol not in {symbol.name for symbol in parsed.symbols}:
                raise RuntimeError(
                    f"named declaration {expected_symbol!r} was not preserved; "
                    f"symbols={[symbol.name for symbol in parsed.symbols]}"
                )
            extraction[spec.name] = {
                "symbols": len(parsed.symbols),
                "chunks": len(parsed.chunks),
                "references": len(parsed.references),
            }
            validated.append(spec.name)
        except Exception as exc:
            failures.append({"language": spec.name, "grammar": spec.grammar_name, "reason": f"{type(exc).__name__}: {exc}"})
    return {
        **profile,
        "status": "ok" if not failures else "failed",
        "validated": validated,
        "extraction": extraction,
        "failures": failures,
    }
