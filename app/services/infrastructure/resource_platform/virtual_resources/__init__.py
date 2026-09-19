"""VRN 公共出口：grammar、纯值对象与 typed resolver。"""

from __future__ import annotations

from app.services.infrastructure.resource_platform.sources.observed_source import (
    ObservedSourceDescriptor,
)
from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    ParsedVrn,
    VrnGrammarError,
    memory_display_uri,
    parse_vrn,
    skill_display_uri,
    workspace_agent_spec_display_uri,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    CatalogBinding,
    TrackedResourceBinding,
    VirtualResourceCatalog,
    VirtualResourceResolver,
    VrnResolveError,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    OPERATION_ACTIVATE,
    OPERATION_OBSERVE,
    OPERATION_READ_CONTENT,
    ResolutionContext,
    ResolvedResourceHandle,
    ResourceProvenance,
    SemanticResourceDescriptor,
)

__all__ = [
    "OPERATION_ACTIVATE",
    "OPERATION_OBSERVE",
    "OPERATION_READ_CONTENT",
    "CatalogBinding",
    "ObservedSourceDescriptor",
    "ParsedVrn",
    "ResolutionContext",
    "ResolvedResourceHandle",
    "ResourceProvenance",
    "SemanticResourceDescriptor",
    "TrackedResourceBinding",
    "VirtualResourceCatalog",
    "VirtualResourceResolver",
    "VrnGrammarError",
    "VrnResolveError",
    "memory_display_uri",
    "parse_vrn",
    "skill_display_uri",
    "workspace_agent_spec_display_uri",
]
