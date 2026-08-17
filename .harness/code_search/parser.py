from __future__ import annotations

import ast
import bisect
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .languages import LanguageSpec, detect_language, load_parser, parser_runtime_profile
from .models import CodeChunk, CodeReference, CodeSymbol, ParsedFile

MAX_CHUNK_CHARS = 6000
MIN_CHUNK_CHARS = 120
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="ignore")
    return hashlib.sha256(value).hexdigest()


def _line_offsets(data: bytes) -> list[int]:
    offsets = [0]
    for index, value in enumerate(data):
        if value == 10:
            offsets.append(index + 1)
    return offsets


def _byte_line(offsets: list[int], offset: int, *, exclusive_end: bool = False) -> int:
    """Convert a byte offset to a one-based source line.

    Tree-sitter end offsets are exclusive. When an ending offset points at the
    beginning of the next line, attribute it to the preceding source line.
    """
    value = max(0, int(offset))
    if exclusive_end and value > 0:
        value -= 1
    return max(1, bisect.bisect_right(offsets, value))


def _slice_text(data: bytes, start: int, end: int) -> str:
    return data[max(0, start):max(start, end)].decode("utf-8", errors="replace")


def _node_text(data: bytes, node: Any) -> str:
    return _slice_text(data, int(node.start_byte), int(node.end_byte))


def _point_line(node: Any, *, end: bool = False) -> int:
    point = node.end_point if end else node.start_point
    try:
        row = point.row
    except AttributeError:
        row = point[0]
    return int(row) + 1


def _point_column(node: Any) -> int:
    point = node.start_point
    try:
        return int(point.column)
    except AttributeError:
        return int(point[1])


def _named_children(node: Any) -> Iterable[Any]:
    value = getattr(node, "named_children", None)
    if value is not None:
        return value
    return [child for child in getattr(node, "children", ()) if getattr(child, "is_named", True)]


def _child_field(node: Any, *names: str) -> Any | None:
    method = getattr(node, "child_by_field_name", None)
    if method is None:
        return None
    for name in names:
        try:
            child = method(name)
        except Exception:
            child = None
        if child is not None:
            return child
    return None


def _identifier_from_text(text: str) -> str:
    identifiers = _IDENTIFIER_RE.findall(text)
    return identifiers[-1] if identifiers else text.strip()[:160]


_NAME_NODE_TYPES = {
    "identifier",
    "field_identifier",
    "name",
    "namespace_identifier",
    "operator_name",
    "property_identifier",
    "shorthand_property_identifier",
    "type_identifier",
}


_DECLARATION_WRAPPER_MARKERS = (
    "declaration",
    "declarator",
    "definition",
    "specifier",
    "type_spec",
    "type_parameter",
    "item",
)

_DECLARATION_DESCENT_BLOCKLIST = (
    "argument",
    "body",
    "block",
    "initializer",
    "parameter",
    "value",
)


def _is_declaration_wrapper(node_type: str) -> bool:
    """Return whether a grammar node may wrap a declaration owner.

    Tree-sitter grammars disagree on where named declaration owners live. Go,
    for example, wraps a named ``type_spec`` below ``type_declaration`` while
    Java/C#/TypeScript commonly expose the owner directly on the declaration.
    Descend only through declaration-shaped wrappers and explicitly avoid
    bodies/parameters/initializers so a parameter identifier can never become
    the declaration's symbol name.
    """
    lowered = str(node_type or "").lower()
    if any(marker in lowered for marker in _DECLARATION_DESCENT_BLOCKLIST):
        return False
    return any(marker in lowered for marker in _DECLARATION_WRAPPER_MARKERS)


def _declarator_name(data: bytes, node: Any, *, depth: int = 0) -> str:
    """Resolve a declaration name without treating parameter names as owners.

    C/C++ grammars commonly expose an entire ``function_declarator`` through
    the declaration's ``declarator`` field. Taking the final identifier from
    that text returns the final parameter name rather than the function. Walk
    the grammar's owner-bearing fields first and only use bounded textual
    fallback after structural candidates are exhausted.
    """
    if node is None or depth > 12:
        return ""
    node_type = str(getattr(node, "type", ""))
    if node_type in _NAME_NODE_TYPES:
        return _identifier_from_text(_node_text(data, node).strip())

    for field in ("name", "declarator", "field", "property", "left"):
        child = _child_field(node, field)
        if child is None or child is node:
            continue
        resolved = _declarator_name(data, child, depth=depth + 1)
        if resolved:
            return resolved

    # Qualified C++/JavaScript names may not expose a stable field across all
    # bundled grammar versions. Prefer identifier-shaped direct children from
    # left to right; never scan parameter-list descendants here.
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if child_type in _NAME_NODE_TYPES:
            text = _node_text(data, child).strip()
            if text:
                return _identifier_from_text(text)

    # Some grammars put the owner one declaration-wrapper below the node Awoki
    # indexes (for example Go ``type_declaration -> type_spec -> name``). Keep
    # this syntax-agnostic and bounded: recurse only into declaration-shaped
    # wrappers, never arbitrary descendants. This preserves real source names
    # such as MatchingEngine instead of degrading them to ``anonymous_<line>``
    # without introducing per-language naming dictionaries.
    for child in _named_children(node):
        child_type = str(getattr(child, "type", ""))
        if not _is_declaration_wrapper(child_type):
            continue
        resolved = _declarator_name(data, child, depth=depth + 1)
        if resolved:
            return resolved
    return ""


def _symbol_name(data: bytes, node: Any) -> str:
    resolved = _declarator_name(data, node)
    if resolved:
        return resolved

    # Assignment/export wrappers own anonymous function expressions and arrow
    # functions. Resolve their explicit binding rather than naming parameters.
    parent = getattr(node, "parent", None)
    if parent is not None:
        candidate = _child_field(parent, "name", "left", "declarator")
        resolved = _declarator_name(data, candidate)
        if resolved:
            return resolved

    return f"anonymous_{_point_line(node)}"


def _symbol_kind(node_type: str, spec: LanguageSpec) -> str:
    if node_type in spec.class_types:
        lowered = node_type.lower()
        for kind in ("interface", "trait", "enum", "struct", "record", "module", "namespace", "class", "type"):
            if kind in lowered:
                return kind
        return "type"
    lowered = node_type.lower()
    if "constructor" in lowered:
        return "constructor"
    if "method" in lowered:
        return "method"
    return "function"


def _signature(data: bytes, node: Any) -> str:
    text = _node_text(data, node).strip()
    if not text:
        return ""
    body = _child_field(node, "body")
    if body is not None and int(body.start_byte) > int(node.start_byte):
        text = _slice_text(data, int(node.start_byte), int(body.start_byte)).strip()
    first = text.splitlines()[0].strip()
    return first[:1000]


def _extend_leading_comments(data: bytes, node: Any) -> int:
    start = int(node.start_byte)
    sibling = getattr(node, "prev_named_sibling", None)
    while sibling is not None and getattr(sibling, "type", "") in {"comment", "line_comment", "block_comment"}:
        gap = _slice_text(data, int(sibling.end_byte), start)
        if gap.count("\n") > 2 or gap.strip():
            break
        start = int(sibling.start_byte)
        sibling = getattr(sibling, "prev_named_sibling", None)
    return start


_TRANSPARENT_DEFINITION_WRAPPERS = {
    # These nodes wrap one declaration without changing the declaration's
    # lexical ownership. Include them in the searchable chunk so decorators
    # and export modifiers are not lost, while keeping the symbol's own source
    # line for deterministic definition/claim resolution.
    "decorated_definition",
    "export_statement",
}


def _definition_chunk_span(data: bytes, node: Any) -> tuple[int, int]:
    owner = node
    parent = getattr(owner, "parent", None)
    while parent is not None and str(getattr(parent, "type", "")) in _TRANSPARENT_DEFINITION_WRAPPERS:
        owner = parent
        parent = getattr(owner, "parent", None)
    return _extend_leading_comments(data, owner), int(owner.end_byte)


def _without_ranges(data: bytes, start: int, end: int, ranges: Iterable[tuple[int, int]]) -> str:
    """Return a source slice with nested declarations removed line-preservingly.

    Only newline bytes are retained from removed ranges. This prevents class or
    outer-function chunks from duplicating complete nested symbol bodies while
    keeping every later line number aligned with the original file.
    """
    cursor = max(0, start)
    limit = max(cursor, end)
    parts: list[bytes] = []
    for range_start, range_end in sorted(ranges):
        clipped_start = max(cursor, min(limit, int(range_start)))
        clipped_end = max(clipped_start, min(limit, int(range_end)))
        if clipped_end <= cursor:
            continue
        parts.append(data[cursor:clipped_start])
        parts.append(b"\n" * data[clipped_start:clipped_end].count(b"\n"))
        cursor = clipped_end
    parts.append(data[cursor:limit])
    return b"".join(parts).decode("utf-8", errors="replace")


def _module_name(path: str) -> str:
    value = Path(path).with_suffix("").as_posix().strip("/")
    return value.replace("/", ".") or "module"


def _embedding_key(content_hash: str, embedding_profile_hash: str) -> str:
    return _sha256(f"{embedding_profile_hash}|{content_hash}")


def _chunk_parts(
    *,
    path: str,
    symbol_id: str | None,
    symbol_name: str,
    qualified_name: str,
    symbol_kind: str,
    start_line: int,
    text: str,
    embedding_profile_hash: str,
) -> list[CodeChunk]:
    text = text.strip("\n")
    if not text.strip():
        return []
    lines = text.splitlines(keepends=True)
    groups: list[tuple[int, int, str]] = []
    current: list[str] = []
    current_start = 0
    count = 0
    for index, line in enumerate(lines):
        if current and count + len(line) > MAX_CHUNK_CHARS:
            groups.append((current_start, index, "".join(current)))
            current = []
            current_start = index
            count = 0
        current.append(line)
        count += len(line)
    if current:
        groups.append((current_start, len(lines), "".join(current)))
    out: list[CodeChunk] = []
    total = len(groups)
    for part, (line_start, line_end, body) in enumerate(groups, start=1):
        if len(body.strip()) < MIN_CHUNK_CHARS and total > 1 and out:
            previous = out.pop()
            merged = previous.text.rstrip() + "\n" + body
            content_hash = _sha256(merged)
            out.append(CodeChunk(
                chunk_id=_sha256(f"{path}|{symbol_id}|{previous.chunk_part}|{content_hash}"),
                symbol_id=symbol_id,
                symbol_name=symbol_name,
                qualified_name=qualified_name,
                symbol_kind=symbol_kind,
                chunk_part=previous.chunk_part,
                chunk_total=total - 1,
                start_line=previous.start_line,
                end_line=start_line + line_end - 1,
                title=f"{symbol_kind}: {qualified_name}",
                text=merged,
                content_hash=content_hash,
                embedding_key=_embedding_key(content_hash, embedding_profile_hash),
            ))
            continue
        content_hash = _sha256(body)
        out.append(CodeChunk(
            chunk_id=_sha256(f"{path}|{symbol_id or 'module'}|{part}|{content_hash}"),
            symbol_id=symbol_id,
            symbol_name=symbol_name,
            qualified_name=qualified_name,
            symbol_kind=symbol_kind,
            chunk_part=part,
            chunk_total=total,
            start_line=start_line + line_start,
            end_line=start_line + max(line_start, line_end - 1),
            title=f"{symbol_kind}: {qualified_name}",
            text=body,
            content_hash=content_hash,
            embedding_key=_embedding_key(content_hash, embedding_profile_hash),
        ))
    if out:
        final_total = len(out)
        out = [CodeChunk(**{**chunk.__dict__, "chunk_total": final_total}) for chunk in out]
    return out


def _containing_symbol(symbol_nodes: list[tuple[Any, CodeSymbol]], node: Any) -> CodeSymbol | None:
    start, end = int(node.start_byte), int(node.end_byte)
    candidates = [symbol for owner, symbol in symbol_nodes if int(owner.start_byte) <= start and int(owner.end_byte) >= end]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.end_byte - item.start_byte)


def _call_anchor_node(data: bytes, candidate: Any, target: str) -> Any:
    """Return the syntax occurrence that should own call-reference identity.

    Several Tree-sitter grammars represent chained calls as nested call nodes
    whose start position is the beginning of the whole receiver chain.  Using
    that outer-node position makes distinct calls such as ``x.map(...).map(...)``
    collide.  Prefer the right-most identifier-shaped descendant matching the
    resolved target; fall back to the grammar-provided candidate when a grammar
    does not expose a narrower name token.
    """
    best = None
    stack = [candidate]
    while stack:
        current = stack.pop()
        current_type = str(getattr(current, "type", ""))
        if current_type in _NAME_NODE_TYPES:
            text = _identifier_from_text(_node_text(data, current).strip())
            if text == target:
                if best is None or (
                    int(getattr(current, "start_byte", -1)),
                    int(getattr(current, "end_byte", -1)),
                ) > (
                    int(getattr(best, "start_byte", -1)),
                    int(getattr(best, "end_byte", -1)),
                ):
                    best = current
        stack.extend(_named_children(current))
    return best if best is not None else candidate


def _call_target(data: bytes, node: Any) -> tuple[str, str, Any]:
    candidate = _child_field(node, "function", "name", "method", "constructor", "type", "command")
    if candidate is None:
        children = list(_named_children(node))
        candidate = children[0] if children else node
    text = _node_text(data, candidate).strip()
    normalized = re.sub(r"\s+", "", text)[:500]
    target = _identifier_from_text(normalized)
    return target, normalized, _call_anchor_node(data, candidate, target)


def _module_hint(value: str) -> str:
    normalized = value.strip().strip("'\"").replace("\\", "/")
    normalized = re.sub(r"\.(?:py|js|jsx|ts|tsx|mjs|cjs)$", "", normalized)
    normalized = normalized.lstrip("./").replace("/", ".")
    return normalized.strip(".")


def _import_bindings(language: str, source: str) -> list[tuple[str, str]]:
    """Extract deterministic visible-name to qualified-target import bindings.

    Tree-sitter import node shapes differ by grammar. Parsing the bounded import
    statement text here keeps alias resolution explicit and testable instead of
    guessing from the last identifier in an entire statement.
    """
    text = re.sub(r"\s+", " ", source.strip())
    bindings: list[tuple[str, str]] = []
    if language == "python":
        direct = re.match(r"^import\s+(.+)$", text)
        if direct:
            for item in direct.group(1).split(","):
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?$", item.strip())
                if not match:
                    continue
                qualified = match.group(1)
                visible = match.group(2) or qualified.split(".", 1)[0]
                bindings.append((visible, qualified))
            return bindings
        imported = re.match(r"^from\s+([.A-Za-z_][A-Za-z0-9_.]*)\s+import\s+(.+)$", text)
        if imported:
            module = imported.group(1).strip(".")
            names = imported.group(2).strip().strip("()")
            for item in names.split(","):
                match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?$", item.strip())
                if not match or match.group(1) == "*":
                    continue
                original = match.group(1)
                visible = match.group(2) or original
                bindings.append((visible, ".".join(part for part in (module, original) if part)))
            return bindings
    if language in {"javascript", "typescript", "tsx"}:
        module_match = re.search(r"\bfrom\s+['\"]([^'\"]+)['\"]", text)
        side_effect = re.match(r"^import\s+['\"]([^'\"]+)['\"]", text)
        module = _module_hint(module_match.group(1) if module_match else side_effect.group(1) if side_effect else "")
        if not module:
            return []
        namespace = re.search(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", text)
        if namespace:
            bindings.append((namespace.group(1), module))
        named = re.search(r"\{([^}]*)\}", text)
        if named:
            for item in named.group(1).split(","):
                match = re.match(r"^([A-Za-z_$][A-Za-z0-9_$]*)(?:\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*))?$", item.strip())
                if not match:
                    continue
                original = match.group(1)
                visible = match.group(2) or original
                bindings.append((visible, f"{module}.{original}"))
        prefix = re.match(r"^import\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?:,|from)", text)
        if prefix:
            bindings.append((prefix.group(1), module))
        return bindings
    if language == "java":
        match = re.match(r"^import\s+(?:static\s+)?([A-Za-z_$][A-Za-z0-9_$.]*)(?:\.\*)?\s*;?$", text)
        if match:
            qualified = match.group(1)
            return [(qualified.rsplit(".", 1)[-1], qualified)]
    if language == "csharp":
        alias = re.match(r"^using\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)\s*;?$", text)
        if alias:
            return [(alias.group(1), alias.group(2))]
    return bindings


def _contains_node(container: Any, candidate: Any) -> bool:
    return (
        int(getattr(container, "start_byte", -1)) <= int(getattr(candidate, "start_byte", -2))
        and int(getattr(container, "end_byte", -1)) >= int(getattr(candidate, "end_byte", -2))
    )


def _tree_sitter_control_context(data: bytes, node: Any, spec: LanguageSpec) -> tuple[str, ...]:
    """Return enclosing static control predicates from inner to outer.

    This is evidence about the source path containing the reference, not a
    runtime assertion that the predicate is satisfiable or that the edge runs.
    """
    contexts: list[str] = []
    current = node
    parent = getattr(node, "parent", None)
    while parent is not None:
        node_type = str(getattr(parent, "type", ""))
        if node_type in spec.branch_types:
            # Keep control-context labels stable across Tree-sitter grammars.
            # Python exposes `if_statement`, while the public/static graph
            # contract uses the language-level construct name (`if`).
            context_type = re.sub(r"_(?:statement|expression)$", "", node_type)
            condition = _child_field(parent, "condition", "value", "subject")
            condition_text = _node_text(data, condition if condition is not None else parent).strip()
            condition_text = re.sub(r"\s+", " ", condition_text)[:1000]
            consequence = _child_field(parent, "consequence", "body")
            alternative = _child_field(parent, "alternative")
            if alternative is not None and _contains_node(alternative, current):
                label = f"alternative of {context_type}: {condition_text}"
            elif consequence is not None and _contains_node(consequence, current):
                label = f"{context_type}: {condition_text}"
            else:
                label = f"within {context_type}: {condition_text}"
            contexts.append(label)
        elif node_type in {"try_statement", "try_expression", "except_clause", "catch_clause", "finally_clause", "with_statement"}:
            text = re.sub(r"\s+", " ", _node_text(data, parent).strip())[:500]
            contexts.append(f"within {node_type}: {text}")
        current = parent
        parent = getattr(parent, "parent", None)
    return tuple(contexts)


def _tree_sitter_parse(path: str, data: bytes, spec: LanguageSpec, embedding_profile_hash: str) -> ParsedFile:
    parser = load_parser(spec)
    tree = parser.parse(data)
    root = tree.root_node
    line_offsets = _line_offsets(data)
    diagnostics: list[str] = []
    if getattr(root, "has_error", False):
        diagnostics.append("tree_contains_parse_errors")

    module = _module_name(path)
    module_symbol = CodeSymbol(
        symbol_id=_sha256(f"{path}|{module}|module|{_sha256(data)}"),
        name=Path(path).name,
        qualified_name=f"module:{module}",
        kind="module",
        parent_symbol_id=None,
        start_byte=0,
        end_byte=len(data),
        start_line=1,
        end_line=_byte_line(line_offsets, len(data), exclusive_end=True),
        signature=f"module {module}",
        content_hash=_sha256(data),
    )
    symbol_nodes: list[tuple[Any, CodeSymbol]] = [(root, module_symbol)]
    symbol_chunk_spans: dict[str, tuple[int, int]] = {module_symbol.symbol_id: (0, len(data))}
    symbol_stack: list[tuple[Any, CodeSymbol]] = [(root, module_symbol)]

    def visit_symbols(node: Any) -> None:
        while symbol_stack and int(node.start_byte) >= int(symbol_stack[-1][0].end_byte):
            symbol_stack.pop()
        node_type = str(getattr(node, "type", ""))
        is_symbol = node_type in spec.definition_types or node_type in spec.class_types
        current: CodeSymbol | None = None
        if is_symbol:
            name = _symbol_name(data, node)
            parent = symbol_stack[-1][1] if symbol_stack else None
            qualified = (
                f"{module}.{name}"
                if parent is None or parent.kind == "module"
                else f"{parent.qualified_name}.{name}"
            )
            # Keep the symbol occurrence anchored to the declaration node.
            # Search chunks may start earlier to include decorators/comments,
            # but deterministic definition and claim lookup needs the actual
            # declaration line rather than a preceding comment line.
            declaration_start = int(node.start_byte)
            declaration_end = int(node.end_byte)
            body_text = _slice_text(data, declaration_start, declaration_end)
            content_hash = _sha256(body_text)
            symbol_id = _sha256(f"{path}|{qualified}|{_point_line(node)}|{content_hash}")
            symbol_kind = _symbol_kind(node_type, spec)
            # Python Tree-sitter represents class methods with the generic
            # `function_definition` node type. Preserve lexical ownership:
            # functions directly owned by a type are methods, while nested
            # functions inside functions/methods remain functions.
            if (
                symbol_kind == "function"
                and parent is not None
                and parent.kind in {"class", "interface", "trait", "enum", "struct", "record", "type"}
            ):
                symbol_kind = "method"
            current = CodeSymbol(
                symbol_id=symbol_id,
                name=name,
                qualified_name=qualified,
                kind=symbol_kind,
                parent_symbol_id=parent.symbol_id if parent else None,
                start_byte=declaration_start,
                end_byte=declaration_end,
                start_line=_point_line(node),
                end_line=_byte_line(line_offsets, declaration_end, exclusive_end=True),
                signature=_signature(data, node),
                content_hash=content_hash,
            )
            symbol_nodes.append((node, current))
            symbol_chunk_spans[symbol_id] = _definition_chunk_span(data, node)
            symbol_stack.append((node, current))
        for child in _named_children(node):
            visit_symbols(child)
        if current is not None and symbol_stack and symbol_stack[-1][1].symbol_id == current.symbol_id:
            symbol_stack.pop()

    visit_symbols(root)

    chunks: list[CodeChunk] = []
    symbols_only = [symbol for _, symbol in symbol_nodes if symbol.kind != "module"]
    for _, symbol in symbol_nodes:
        if symbol.kind == "module":
            continue
        chunk_start, chunk_end = symbol_chunk_spans[symbol.symbol_id]
        nested_ranges = [
            symbol_chunk_spans[child.symbol_id]
            for child in symbols_only
            if child.parent_symbol_id == symbol.symbol_id
        ]
        body = _without_ranges(data, chunk_start, chunk_end, nested_ranges)
        chunks.extend(_chunk_parts(
            path=path,
            symbol_id=symbol.symbol_id,
            symbol_name=symbol.name,
            qualified_name=symbol.qualified_name,
            symbol_kind=symbol.kind,
            start_line=_byte_line(line_offsets, chunk_start),
            text=body,
            embedding_profile_hash=embedding_profile_hash,
        ))

    # Build the module chunk from the original file with top-level declaration
    # ranges removed. Newlines are retained, so module-level result line ranges
    # continue to identify the real source even when declarations are far apart.
    top_level_ranges = [
        symbol_chunk_spans[symbol.symbol_id]
        for symbol in symbols_only
        if symbol.parent_symbol_id == module_symbol.symbol_id
    ]
    module_text = _without_ranges(data, 0, len(data), top_level_ranges)
    if module_text.strip() or not chunks:
        chunks.extend(_chunk_parts(
            path=path,
            symbol_id=module_symbol.symbol_id,
            symbol_name=module_symbol.name,
            qualified_name=module_symbol.qualified_name,
            symbol_kind="module",
            start_line=1,
            text=module_text if module_text.strip() else data.decode("utf-8", errors="replace"),
            embedding_profile_hash=embedding_profile_hash,
        ))

    references: list[CodeReference] = []

    def append_reference(
        node: Any,
        *,
        kind: str,
        target: str,
        hint: str,
        source_text: str,
        anchor_node: Any | None = None,
    ) -> None:
        source_symbol = _containing_symbol(symbol_nodes, node)
        occurrence = anchor_node if anchor_node is not None else node
        reference_id = _sha256(
            f"{path}|{source_symbol.symbol_id if source_symbol else ''}|{kind}|{target}|{_point_line(occurrence)}|{_point_column(occurrence)}|{hint}"
        )
        references.append(CodeReference(
            reference_id=reference_id,
            source_symbol_id=source_symbol.symbol_id if source_symbol else None,
            reference_kind=kind,
            target_name=target,
            target_qualified_hint=hint,
            line=_point_line(occurrence),
            column=_point_column(occurrence),
            source_text=source_text[:4000],
            control_context=_tree_sitter_control_context(data, node, spec),
        ))

    def visit_refs(node: Any) -> None:
        node_type = str(getattr(node, "type", ""))
        kind = ""
        target = ""
        hint = ""
        anchor_node = None
        if node_type in spec.call_types:
            kind = "call"
            target, hint, anchor_node = _call_target(data, node)
        elif node_type in spec.import_types:
            source_text = _node_text(data, node).strip()
            bindings = _import_bindings(spec.name, source_text)
            if bindings:
                for visible, qualified in bindings:
                    append_reference(
                        node,
                        kind="import",
                        target=visible,
                        hint=qualified,
                        source_text=source_text,
                    )
            else:
                hint = re.sub(r"\s+", " ", source_text)[:1000]
                append_reference(
                    node,
                    kind="import",
                    target=_identifier_from_text(hint),
                    hint=hint,
                    source_text=source_text,
                )
            kind = ""
        elif node_type in spec.branch_types:
            kind = "branch"
            candidate = _child_field(node, "condition", "value")
            hint = _node_text(data, candidate if candidate is not None else node).strip()[:2000]
            target = node_type
        elif node_type in spec.return_types:
            kind = "return"
            hint = _node_text(data, node).strip()[:2000]
            target = "return"
        elif node_type in spec.raise_types:
            kind = "raise"
            hint = _node_text(data, node).strip()[:2000]
            target = "raise"
        if kind:
            append_reference(
                node,
                kind=kind,
                target=target,
                hint=hint,
                source_text=_node_text(data, node).strip(),
                anchor_node=anchor_node,
            )
        for child in _named_children(node):
            visit_refs(child)

    visit_refs(root)
    runtime = parser_runtime_profile()
    parser_id = f"tree-sitter-language-pack:{runtime['version']}:{spec.grammar_name}:awoki-curated-v1"
    return ParsedFile(
        language=spec.name,
        parser_id=parser_id,
        parse_mode="tree_sitter",
        parse_status="partial" if diagnostics else "ok",
        diagnostics=tuple(diagnostics),
        symbols=tuple(symbol for _, symbol in symbol_nodes),
        chunks=tuple(chunks),
        references=tuple(references),
    )


@dataclass
class _AstOwner:
    node: ast.AST
    symbol: CodeSymbol


def _ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_ast_name(node.value)}.{node.attr}".strip(".")
    if isinstance(node, ast.Call):
        return _ast_name(node.func)
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _python_ast_parse(path: str, data: bytes, embedding_profile_hash: str, reason: str) -> ParsedFile:
    text = data.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=path, type_comments=True)
    except SyntaxError as exc:
        return _text_fallback(path, data, embedding_profile_hash, "python", f"python_ast_error:{exc.msg}")
    module = _module_name(path)
    source_lines = text.splitlines(keepends=True)
    line_offsets = _line_offsets(data)
    module_symbol = CodeSymbol(
        symbol_id=_sha256(f"{path}|{module}|module|{_sha256(data)}"),
        name=Path(path).name,
        qualified_name=f"module:{module}",
        kind="module",
        parent_symbol_id=None,
        start_byte=0,
        end_byte=len(data),
        start_line=1,
        end_line=max(1, len(source_lines)),
        signature=f"module {module}",
        content_hash=_sha256(data),
    )
    owners: list[_AstOwner] = [_AstOwner(tree, module_symbol)]
    ast_chunk_spans: dict[str, tuple[int, int]] = {module_symbol.symbol_id: (0, len(data))}
    stack: list[CodeSymbol] = [module_symbol]

    class Visitor(ast.NodeVisitor):
        def _visit_symbol(self, node: ast.AST, name: str, kind: str) -> None:
            parent = stack[-1] if stack else None
            qualified = (
                f"{module}.{name}"
                if parent is None or parent.kind == "module"
                else f"{parent.qualified_name}.{name}"
            )
            start_line = int(getattr(node, "lineno", 1))
            end_line = int(getattr(node, "end_lineno", start_line))
            lines = text.splitlines(keepends=True)
            source = "".join(lines[start_line - 1:end_line])
            content_hash = _sha256(source)
            symbol = CodeSymbol(
                symbol_id=_sha256(f"{path}|{qualified}|{start_line}|{content_hash}"),
                name=name,
                qualified_name=qualified,
                kind=kind,
                parent_symbol_id=parent.symbol_id if parent else None,
                start_byte=len("".join(lines[:start_line - 1]).encode("utf-8")),
                end_byte=len("".join(lines[:end_line]).encode("utf-8")),
                start_line=start_line,
                end_line=end_line,
                signature=(source.splitlines()[0].strip() if source else "")[:1000],
                content_hash=content_hash,
            )
            decorators = list(getattr(node, "decorator_list", []) or [])
            chunk_start_line = min(
                [start_line, *[int(getattr(item, "lineno", start_line)) for item in decorators]]
            )
            ast_chunk_spans[symbol.symbol_id] = (
                len("".join(lines[:chunk_start_line - 1]).encode("utf-8")),
                symbol.end_byte,
            )
            owners.append(_AstOwner(node, symbol))
            stack.append(symbol)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_symbol(node, node.name, "method" if stack and stack[-1].kind == "class" else "function")

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_symbol(node, node.name, "method" if stack and stack[-1].kind == "class" else "function")

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_symbol(node, node.name, "class")

    Visitor().visit(tree)
    lines = text.splitlines(keepends=True)
    chunks: list[CodeChunk] = []
    symbols_only = [owner.symbol for owner in owners if owner.symbol.kind != "module"]
    for owner in owners:
        if owner.symbol.kind == "module":
            continue
        chunk_start, chunk_end = ast_chunk_spans[owner.symbol.symbol_id]
        nested_ranges = [
            ast_chunk_spans[child.symbol_id]
            for child in symbols_only
            if child.parent_symbol_id == owner.symbol.symbol_id
        ]
        source = _without_ranges(data, chunk_start, chunk_end, nested_ranges)
        chunks.extend(_chunk_parts(
            path=path,
            symbol_id=owner.symbol.symbol_id,
            symbol_name=owner.symbol.name,
            qualified_name=owner.symbol.qualified_name,
            symbol_kind=owner.symbol.kind,
            start_line=_byte_line(line_offsets, chunk_start),
            text=source,
            embedding_profile_hash=embedding_profile_hash,
        ))
    top_level_ranges = [
        ast_chunk_spans[symbol.symbol_id]
        for symbol in symbols_only
        if symbol.parent_symbol_id == module_symbol.symbol_id
    ]
    module_text = _without_ranges(data, 0, len(data), top_level_ranges)
    if module_text.strip() or not chunks:
        chunks.extend(_chunk_parts(
            path=path,
            symbol_id=module_symbol.symbol_id,
            symbol_name=module_symbol.name,
            qualified_name=module_symbol.qualified_name,
            symbol_kind="module",
            start_line=1,
            text=module_text if module_text.strip() else text,
            embedding_profile_hash=embedding_profile_hash,
        ))

    def containing(node: ast.AST) -> CodeSymbol | None:
        line = int(getattr(node, "lineno", 0))
        candidates = [owner.symbol for owner in owners if owner.symbol.start_line <= line <= owner.symbol.end_line]
        return min(candidates, key=lambda item: item.end_line - item.start_line) if candidates else None

    parent_of: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[child] = parent

    def python_control_context(node: ast.AST) -> tuple[str, ...]:
        contexts: list[str] = []
        current = node
        parent = parent_of.get(current)
        while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if isinstance(parent, ast.If):
                condition = ast.get_source_segment(text, parent.test) or ast.unparse(parent.test)
                direct = current
                if direct in parent.orelse:
                    contexts.append(f"else of if: {condition}")
                else:
                    contexts.append(f"if: {condition}")
            elif isinstance(parent, (ast.While,)):
                condition = ast.get_source_segment(text, parent.test) or ast.unparse(parent.test)
                contexts.append(f"while: {condition}")
            elif isinstance(parent, (ast.For, ast.AsyncFor)):
                target = ast.get_source_segment(text, parent.target) or ast.unparse(parent.target)
                iterator = ast.get_source_segment(text, parent.iter) or ast.unparse(parent.iter)
                contexts.append(f"for: {target} in {iterator}")
            elif isinstance(parent, ast.Match):
                subject = ast.get_source_segment(text, parent.subject) or ast.unparse(parent.subject)
                contexts.append(f"match: {subject}")
            elif isinstance(parent, ast.ExceptHandler):
                exception = ast.get_source_segment(text, parent.type) if parent.type is not None else "any exception"
                contexts.append(f"except: {exception or 'any exception'}")
            elif isinstance(parent, ast.Try):
                contexts.append("within try statement")
            elif isinstance(parent, (ast.With, ast.AsyncWith)):
                contexts.append("within context manager")
            current = parent
            parent = parent_of.get(current)
        return tuple(contexts)

    references: list[CodeReference] = []

    def add_reference(
        node: ast.AST,
        *,
        kind: str,
        target: str,
        hint: str,
        source_text: str | None = None,
    ) -> None:
        owner = containing(node)
        line = int(getattr(node, "lineno", 1))
        column = int(getattr(node, "col_offset", 0))
        observed = source_text or ast.get_source_segment(text, node) or hint
        references.append(CodeReference(
            reference_id=_sha256(f"{path}|{owner.symbol_id if owner else ''}|{kind}|{target}|{line}|{column}|{hint}"),
            source_symbol_id=owner.symbol_id if owner else None,
            reference_kind=kind,
            target_name=target,
            target_qualified_hint=hint[:2000],
            line=line,
            column=column,
            source_text=observed[:4000],
            control_context=python_control_context(node),
        ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            hint = _ast_name(node.func)
            add_reference(node, kind="call", target=hint.rsplit(".", 1)[-1], hint=hint)
        elif isinstance(node, ast.Import):
            source_text = ast.get_source_segment(text, node) or ast.unparse(node)
            for alias in node.names:
                visible = alias.asname or alias.name.split(".", 1)[0]
                add_reference(node, kind="import", target=visible, hint=alias.name, source_text=source_text)
        elif isinstance(node, ast.ImportFrom):
            source_text = ast.get_source_segment(text, node) or ast.unparse(node)
            module_name = ("." * int(node.level)) + (node.module or "")
            for alias in node.names:
                visible = alias.asname or alias.name
                qualified = f"{module_name}.{alias.name}".strip(".")
                add_reference(node, kind="import", target=visible, hint=qualified, source_text=source_text)
        elif isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Match)):
            condition = getattr(node, "test", getattr(node, "subject", getattr(node, "iter", node)))
            hint = ast.get_source_segment(text, condition) or ast.unparse(condition)
            add_reference(node, kind="branch", target=type(node).__name__, hint=hint)
        elif isinstance(node, ast.Return):
            hint = ast.get_source_segment(text, node) or ast.unparse(node)
            add_reference(node, kind="return", target="return", hint=hint)
        elif isinstance(node, ast.Raise):
            hint = ast.get_source_segment(text, node) or ast.unparse(node)
            add_reference(node, kind="raise", target="raise", hint=hint)
    return ParsedFile(
        language="python",
        parser_id="python-ast:stdlib",
        parse_mode="python_ast_fallback",
        parse_status="ok",
        diagnostics=(reason,),
        symbols=tuple(owner.symbol for owner in owners),
        chunks=tuple(chunks),
        references=tuple(references),
    )


_SMALI_CLASS_RE = re.compile(r"^\s*\.class\b.*\s(?P<descriptor>L[^;]+;)\s*$")
_SMALI_METHOD_RE = re.compile(r"^\s*\.method\b(?P<flags>.*?)\s(?P<name>[^\s(]+)(?P<descriptor>\([^)]*\).+)\s*$")
_SMALI_FIELD_RE = re.compile(r"^\s*\.field\b.*?\s(?P<name>[^\s:=]+):(?P<type>[^\s=]+)")
_SMALI_MEMBER_REF_RE = re.compile(r"(?P<class>L[^;]+;)->(?P<name>[^\s(:]+)(?P<descriptor>\([^)]*\).+|:[^\s,}]+)")


def _smali_parse(path: str, data: bytes, embedding_profile_hash: str) -> ParsedFile:
    """Deterministically parse Smali declarations and bytecode references.

    This parser is intentionally local and dependency-free. It does not compile,
    execute, disassemble, or decompile input. Smali text is treated as primary
    evidence and mapped into Awoki's existing symbol/reference graph.
    """
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line.encode("utf-8", errors="replace"))
    if not lines:
        lines = [""]
        offsets = [0]

    class_descriptor = ""
    class_line = 1
    class_signature = ""
    for index, line in enumerate(lines, start=1):
        match = _SMALI_CLASS_RE.match(line.rstrip("\r\n"))
        if match:
            class_descriptor = match.group("descriptor")
            class_line = index
            class_signature = line.strip()
            break
    if not class_descriptor:
        class_descriptor = f"L{Path(path).with_suffix('').as_posix().strip('/')};"
        class_signature = f".class {class_descriptor}"

    class_hash = _sha256(data)
    class_symbol = CodeSymbol(
        symbol_id=_sha256(f"{path}|{class_descriptor}|class|{class_hash}"),
        name=class_descriptor.rsplit("/", 1)[-1].rstrip(";"),
        qualified_name=class_descriptor,
        kind="class",
        parent_symbol_id=None,
        start_byte=0,
        end_byte=len(data),
        start_line=class_line,
        end_line=max(1, len(lines)),
        signature=class_signature[:1000],
        content_hash=class_hash,
    )

    symbols: list[CodeSymbol] = [class_symbol]
    chunks: list[CodeChunk] = []
    references: list[CodeReference] = []
    diagnostics: list[str] = []
    method_ranges: list[tuple[int, int]] = []
    method_symbols: list[tuple[int, int, CodeSymbol]] = []

    index = 0
    while index < len(lines):
        raw = lines[index].rstrip("\r\n")
        method_match = _SMALI_METHOD_RE.match(raw)
        if method_match:
            start_index = index
            end_index = index
            while end_index + 1 < len(lines) and not re.match(r"^\s*\.end\s+method\b", lines[end_index + 1]):
                end_index += 1
            if end_index + 1 < len(lines):
                end_index += 1
            else:
                diagnostics.append(f"unterminated_method:{index + 1}")
            start_byte = offsets[start_index]
            end_byte = offsets[end_index] + len(lines[end_index].encode("utf-8", errors="replace"))
            name = method_match.group("name")
            descriptor = method_match.group("descriptor").strip()
            qualified = f"{class_descriptor}->{name}{descriptor}"
            body = data[start_byte:end_byte]
            symbol = CodeSymbol(
                symbol_id=_sha256(f"{path}|{qualified}|{start_index + 1}|{_sha256(body)}"),
                name=name,
                qualified_name=qualified,
                kind="method",
                parent_symbol_id=class_symbol.symbol_id,
                start_byte=start_byte,
                end_byte=end_byte,
                start_line=start_index + 1,
                end_line=end_index + 1,
                signature=raw.strip()[:1000],
                content_hash=_sha256(body),
            )
            symbols.append(symbol)
            method_symbols.append((start_index, end_index, symbol))
            method_ranges.append((start_byte, end_byte))
            chunks.extend(_chunk_parts(
                path=path,
                symbol_id=symbol.symbol_id,
                symbol_name=symbol.name,
                qualified_name=symbol.qualified_name,
                symbol_kind=symbol.kind,
                start_line=start_index + 1,
                text=body.decode("utf-8", errors="replace"),
                embedding_profile_hash=embedding_profile_hash,
            ))
            index = end_index + 1
            continue
        field_match = _SMALI_FIELD_RE.match(raw)
        if field_match:
            name = field_match.group("name")
            field_type = field_match.group("type")
            qualified = f"{class_descriptor}->{name}:{field_type}"
            start_byte = offsets[index]
            end_byte = start_byte + len(lines[index].encode("utf-8", errors="replace"))
            body = data[start_byte:end_byte]
            symbol = CodeSymbol(
                symbol_id=_sha256(f"{path}|{qualified}|{index + 1}|{_sha256(body)}"),
                name=name,
                qualified_name=qualified,
                kind="field",
                parent_symbol_id=class_symbol.symbol_id,
                start_byte=start_byte,
                end_byte=end_byte,
                start_line=index + 1,
                end_line=index + 1,
                signature=raw.strip()[:1000],
                content_hash=_sha256(body),
            )
            symbols.append(symbol)
            chunks.extend(_chunk_parts(
                path=path,
                symbol_id=symbol.symbol_id,
                symbol_name=symbol.name,
                qualified_name=symbol.qualified_name,
                symbol_kind=symbol.kind,
                start_line=index + 1,
                text=raw + "\n",
                embedding_profile_hash=embedding_profile_hash,
            ))
        index += 1

    # Keep class-level directives searchable while removing method bodies
    # line-preservingly so a broad class hit does not duplicate every method.
    class_text = _without_ranges(data, 0, len(data), method_ranges)
    if class_text.strip() or not chunks:
        chunks.extend(_chunk_parts(
            path=path,
            symbol_id=class_symbol.symbol_id,
            symbol_name=class_symbol.name,
            qualified_name=class_symbol.qualified_name,
            symbol_kind=class_symbol.kind,
            start_line=1,
            text=class_text if class_text.strip() else text,
            embedding_profile_hash=embedding_profile_hash,
        ))

    def owner_for(line_index: int) -> CodeSymbol:
        for start, end, symbol in method_symbols:
            if start <= line_index <= end:
                return symbol
        return class_symbol

    def add_ref(line_index: int, *, kind: str, target: str, hint: str, source_text: str) -> None:
        owner = owner_for(line_index)
        column = max(0, len(source_text) - len(source_text.lstrip()))
        references.append(CodeReference(
            reference_id=_sha256(f"{path}|{owner.symbol_id}|{kind}|{target}|{line_index + 1}|{column}|{hint}"),
            source_symbol_id=owner.symbol_id,
            reference_kind=kind,
            target_name=target,
            target_qualified_hint=hint[:1000],
            line=line_index + 1,
            column=column,
            source_text=source_text.strip()[:4000],
            control_context=(),
        ))

    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        opcode = stripped.split(None, 1)[0]
        if opcode.startswith("invoke-"):
            match = _SMALI_MEMBER_REF_RE.search(stripped)
            if match and match.group("descriptor").startswith("("):
                hint = f"{match.group('class')}->{match.group('name')}{match.group('descriptor')}"
                add_ref(line_index, kind="call", target=match.group("name"), hint=hint, source_text=line)
        elif opcode.startswith(("iget", "iput", "sget", "sput")):
            match = _SMALI_MEMBER_REF_RE.search(stripped)
            if match:
                hint = f"{match.group('class')}->{match.group('name')}{match.group('descriptor')}"
                add_ref(line_index, kind="data_ref", target=match.group("name"), hint=hint, source_text=line)
        elif opcode in {"new-instance", "check-cast", "instance-of", "const-class"}:
            descriptor = next((part.rstrip(",") for part in stripped.split() if part.startswith("L") and part.rstrip(",").endswith(";")), "")
            if descriptor:
                add_ref(line_index, kind="type_ref", target=descriptor.rsplit("/", 1)[-1].rstrip(";"), hint=descriptor, source_text=line)
        elif opcode.startswith("if-") or opcode.startswith("goto") or opcode in {"packed-switch", "sparse-switch"}:
            add_ref(line_index, kind="branch", target=opcode, hint=stripped, source_text=line)
        elif opcode.startswith("return"):
            add_ref(line_index, kind="return", target="return", hint=stripped, source_text=line)
        elif opcode == "throw":
            add_ref(line_index, kind="raise", target="throw", hint=stripped, source_text=line)

    return ParsedFile(
        language="smali",
        parser_id="awoki-smali-v1",
        parse_mode="smali_structural",
        parse_status="partial" if diagnostics else "ok",
        diagnostics=tuple(diagnostics),
        symbols=tuple(symbols),
        chunks=tuple(chunks),
        references=tuple(references),
    )


def _text_fallback(path: str, data: bytes, embedding_profile_hash: str, language: str, reason: str) -> ParsedFile:
    text = data.decode("utf-8", errors="replace")
    chunks = _chunk_parts(
        path=path,
        symbol_id=None,
        symbol_name=Path(path).name,
        qualified_name=_module_name(path),
        symbol_kind="file",
        start_line=1,
        text=text,
        embedding_profile_hash=embedding_profile_hash,
    )
    return ParsedFile(
        language=language,
        parser_id="deterministic-text-v1",
        parse_mode="text_fallback",
        parse_status="fallback",
        diagnostics=(reason,),
        chunks=tuple(chunks),
    )


def parse_source(path: str, data: bytes, embedding_profile_hash: str) -> ParsedFile:
    spec = detect_language(Path(path))
    if spec is None:
        return _text_fallback(path, data, embedding_profile_hash, "text", "unsupported_language")
    if spec.name == "smali":
        return _smali_parse(path, data, embedding_profile_hash)
    try:
        return _tree_sitter_parse(path, data, spec, embedding_profile_hash)
    except Exception as exc:
        if spec.name == "python":
            return _python_ast_parse(path, data, embedding_profile_hash, f"tree_sitter_unavailable:{type(exc).__name__}")
        return _text_fallback(path, data, embedding_profile_hash, spec.name, f"tree_sitter_unavailable:{type(exc).__name__}")
