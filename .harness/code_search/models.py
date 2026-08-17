from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any




@dataclass(frozen=True)
class SourceRevision:
    """Generic immutable identity for one analyzable evidence-source revision.

    Git repositories are adapted into this shape without changing their legacy
    branch/commit identity. Directory/corpus sources use a deterministic manifest
    hash as ``content_identity`` and ``revision_key``. Legacy branch/repository
    fields remain populated so the existing R9 retrieval pipeline can migrate
    incrementally instead of forking into a second search engine.
    """

    source_id: str
    source_type: str
    revision_key: str
    revision_label: str
    content_identity: str
    dirty: bool
    provenance: dict[str, Any] = field(default_factory=dict)
    repo_id: str = ""
    branch_key: str = ""
    branch_name: str = ""
    commit_sha: str = ""
    source: str = ""

    @classmethod
    def from_branch(cls, branch: "BranchIdentity", *, source_id: str = "") -> "SourceRevision":
        return cls(
            source_id=source_id or branch.repo_id,
            source_type="git",
            revision_key=branch.branch_key,
            revision_label=branch.branch_name,
            content_identity=branch.commit_sha or branch.branch_key,
            dirty=branch.dirty,
            provenance={"identity_source": branch.source},
            repo_id=branch.repo_id,
            branch_key=branch.branch_key,
            branch_name=branch.branch_name,
            commit_sha=branch.commit_sha,
            source=branch.source,
        )


@dataclass(frozen=True)
class EvidenceLocator:
    """Stable locator for source evidence independent of Git line identity.

    Text sources use ``path`` plus an optional symbol and bounded line range.
    Address fields are reserved for later native-binary ingestion; no compiler,
    disassembler, or decompiler integration is implemented in this release.
    """

    source_id: str
    revision_key: str
    path: str = ""
    symbol: str = ""
    start_line: int = 0
    end_line: int = 0
    address_space: str = ""
    start_address: int | None = None
    end_address: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "revision_key": self.revision_key,
            "path": self.path,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "address_space": self.address_space,
            "start_address": self.start_address,
            "end_address": self.end_address,
        }


@dataclass(frozen=True)
class BranchIdentity:
    repo_id: str
    branch_key: str
    branch_name: str
    commit_sha: str
    dirty: bool
    source: str


@dataclass(frozen=True)
class CodeSymbol:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    parent_symbol_id: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    signature: str
    content_hash: str


@dataclass(frozen=True)
class CodeChunk:
    chunk_id: str
    symbol_id: str | None
    symbol_name: str
    qualified_name: str
    symbol_kind: str
    chunk_part: int
    chunk_total: int
    start_line: int
    end_line: int
    title: str
    text: str
    content_hash: str
    embedding_key: str


@dataclass(frozen=True)
class CodeReference:
    reference_id: str
    source_symbol_id: str | None
    reference_kind: str
    target_name: str
    target_qualified_hint: str
    line: int
    column: int
    source_text: str
    control_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedFile:
    language: str
    parser_id: str
    parse_mode: str
    parse_status: str
    diagnostics: tuple[str, ...] = ()
    symbols: tuple[CodeSymbol, ...] = ()
    chunks: tuple[CodeChunk, ...] = ()
    references: tuple[CodeReference, ...] = ()


@dataclass(frozen=True)
class CodeSearchHit:
    project_id: str
    repo_id: str
    branch_key: str
    commit_sha: str
    dirty: bool
    path: str
    language: str
    symbol_id: str | None
    symbol_name: str
    qualified_name: str
    symbol_kind: str
    start_line: int
    end_line: int
    signature: str
    preview: str
    score: float
    source_id: str = ""
    source_type: str = "git"
    revision_key: str = ""
    content_identity: str = ""
    retrieval_backends: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "repo_id": self.repo_id,
            "branch_key": self.branch_key,
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "path": self.path,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "revision_key": self.revision_key,
            "content_identity": self.content_identity,
            "language": self.language,
            "symbol_id": self.symbol_id,
            "symbol": self.symbol_name,
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "preview": self.preview,
            "score": self.score,
            "retrieval_backends": list(self.retrieval_backends),
            "metadata": dict(self.metadata),
        }
