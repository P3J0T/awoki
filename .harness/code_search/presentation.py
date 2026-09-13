"""Lossless-to-the-engine, compact-to-the-model search presentation.

Applied at MCP codebase_search/cross_project_code_search boundaries, AFTER ranking, aggregation and
explicit evidence capture. Internal consumers and captured artifacts retain the
canonical payload. No I/O, backend calls, ranking, source clipping or hit dropping.
Full/diagnostic views and errors pass through untouched. Unknown fields are kept
so a new warning/contract does not silently vanish from the compact presentation.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# Repeated ranking internals, not source/graph/evidence identity or authority.
_HIT_TELEMETRY = set("""
final_rank final_score fused_rank fused_score fts_rank fts_raw_score
lexical_match_mode lexical_normalization_terms qdrant_rank qdrant_raw_score
pre_rerank_rank rerank_rank rerank_score rerank_attempted_for_candidate
rerank_selected rerank_score_returned rerank_scored rerank_backend
rerank_selection_lane rerank_selection_reason pre_rerank_score
rank_fusion_score rank_fusion_components pre_rank_fusion_score
authority_adjustment authority_multiplier authority_relevance_signal
authority_dual_backend_support authority_query_overlap authority_rerank_signal
authority_representation_reserved pre_authority_score diversity_adjustment
diversity_rank_reason focus_composition_original_rank focus_composition_rank_adjustment
focus_composition_relevance_ratio focus_composition_reason promotion_source_rank
promotion_query_overlap refinement_parent_fused_rank refinement_query_overlap
raw_scores
""".split())

# Empty optional metadata need not be repeated on every ordinary discovery hit.
# False/zero values and all unknown fields remain visible.
_OPTIONAL_HIT_FIELDS = set("""
parse_diagnostics signature resolution_status resolution_method candidate_count
confidence line call_source control_context edge promotion_source_symbol_id
promotion_source_path promotion_edge promotion_edge_ids promotion_graph_distance
refinement_parent_symbol_id refinement_parent_path refinement_parent_backends
refinement_depth refinement_enumeration refinement_reason
""".split())


def _retrieval(value: dict[str, Any]) -> dict[str, Any]:
    value = dict(value)
    for key in ("stage_top", "symbol_refinement_diagnostics", "symbol_refinement_limits", "focus_composition"):
        value.pop(key, None)
    # The nested reranker is canonical. Remove only aliases actually represented
    # there; requested/eligible flags and future fields are not guessed away.
    reranker = value.get("reranker")
    if isinstance(reranker, dict):
        for key in list(value):
            if key.startswith("rerank_") and key[len("rerank_"):] in reranker:
                value.pop(key)
    return value


def compact_search_response(result: dict[str, Any], *, view: str = "context") -> dict[str, Any]:
    """Project normal MCP views; detailed views preserve the original contract."""
    selected = (view or "context").lower()
    if selected not in {"peek", "context", "full", "diagnostics"}:
        selected = "context"  # Match the engine's existing normalization.
    if selected in {"full", "diagnostics"} or result.get("status") not in {"ok", "ambiguous", "partial"} or "hits" not in result:
        return result
    compact = deepcopy(result)
    for hit in compact["hits"]:
        for key in _HIT_TELEMETRY:
            hit.pop(key, None)
        for key in _OPTIONAL_HIT_FIELDS:
            if key in hit and hit[key] in (None, "", [], {}):
                hit.pop(key)
    details = compact.get("details")
    if isinstance(details, dict) and isinstance(details.get("retrieval"), dict):
        details["retrieval"] = _retrieval(details["retrieval"])
    for group in ("repositories", "sources"):
        for entry in compact.get(group) or []:
            if isinstance(entry.get("retrieval"), dict):
                entry["retrieval"] = _retrieval(entry["retrieval"])
    compact["presentation"] = {
        "detail": "compact",
        "omitted": "per-hit ranking telemetry and repeated retrieval stage candidates",
        "expanded_view": "full",
        "diagnostic_view": "diagnostics",
        "captured_evidence": "canonical payload before presentation",
    }
    return compact
