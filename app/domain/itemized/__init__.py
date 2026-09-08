"""v2 itemized domain primitives。"""

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import (
    BaseDeltaRole,
    CanonicalItemStatus,
    CommitKind,
    CommitMode,
    ControlOutcome,
    DetailAvailability,
    DetailProtection,
    PayloadKind,
    SelectionKind,
    SemanticKind,
    TurnScope,
    TurnStatus,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    content_hash,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.records import (
    CanonicalItemRecord,
    ProducerRef,
)
from app.domain.itemized.refs import (
    ContextRef,
    ToolSetRef,
    ref_identity,
    require_manifest_token,
    unique_ref_identities,
)
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.runtime import (
    ContentPart,
    ContentPartAnchor,
    ExecutionRecord,
    ItemDraft,
    ModelCallRecord,
    ProvenanceEdge,
    TurnRecord,
)
from app.domain.itemized.schema import (
    validate_item_compatibility,
    validate_selection_compatibility,
)
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serialization import ordered_selection
from app.domain.itemized.validation import validate_turn_transition

__all__ = [
    "BaseDeltaRole",
    "CanonicalItemRecord",
    "CanonicalItemStatus",
    "CommitKind",
    "CommitMode",
    "ContentPart",
    "ContentPartAnchor",
    "ContextAssemblySnapshot",
    "ContextContribution",
    "ContextRef",
    "ContextRequestPlan",
    "ContextSelectionEntry",
    "ControlOutcome",
    "DetailAvailability",
    "DetailProtection",
    "ExecutionRecord",
    "FormatDispatchError",
    "ItemDraft",
    "ItemSchemaError",
    "ModelCallRecord",
    "PayloadKind",
    "ProducerRef",
    "ProvenanceEdge",
    "SelectionKind",
    "SemanticKind",
    "ToolSetRef",
    "TurnRecord",
    "TurnScope",
    "TurnStatus",
    "canonical_json_bytes",
    "content_hash",
    "contribution_content_hash",
    "ordered_selection",
    "ref_identity",
    "require_manifest_token",
    "sha256_jcs",
    "unique_ref_identities",
    "validate_item_compatibility",
    "validate_selection_compatibility",
    "validate_turn_transition",
]
