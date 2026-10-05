"""VRN 公共出口：grammar、纯值对象与 typed resolver。"""

from __future__ import annotations

from app.services.infrastructure.resource_platform.sources.observed_source import (
    ObservedSourceDescriptor,
)
from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    ParsedVrn,
    VrnGrammarError,
    parse_vrn,
    resource_display_uri,
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
    CONTEXT_ASSEMBLY_IDENTITY_NAMESPACE,
    OPERATION_ACTIVATE,
    OPERATION_OBSERVE,
    OPERATION_READ_CONTENT,
    SESSION_RESOURCE_IDENTITY_NAMESPACE,
    ResolutionContext,
    ResolvedResourceHandle,
    ResourceIdentity,
    ResourceProvenance,
    SemanticResourceDescriptor,
    derive_resource_identity,
    validate_resource_identity,
)

__all__ = [
    "CONTEXT_ASSEMBLY_IDENTITY_NAMESPACE",
    "OPERATION_ACTIVATE",
    "OPERATION_OBSERVE",
    "OPERATION_READ_CONTENT",
    "SESSION_RESOURCE_IDENTITY_NAMESPACE",
    "CatalogBinding",
    "ObservedSourceDescriptor",
    "ParsedVrn",
    "ResolutionContext",
    "ResolvedResourceHandle",
    "ResourceIdentity",
    "ResourceProvenance",
    "SemanticResourceDescriptor",
    "TrackedResourceBinding",
    "VirtualResourceCatalog",
    "VirtualResourceResolver",
    "VrnGrammarError",
    "VrnResolveError",
    "derive_resource_identity",
    "parse_vrn",
    "resource_display_uri",
    "skill_display_uri",
    "validate_resource_identity",
    "workspace_agent_spec_display_uri",
]
