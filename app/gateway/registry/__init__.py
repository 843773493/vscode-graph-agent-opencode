from __future__ import annotations

import asyncio as asyncio
import json as json
import os as os
import shutil as shutil
import tempfile as tempfile
from collections.abc import Callable as Callable
from copy import deepcopy as deepcopy
from dataclasses import asdict as asdict
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from datetime import datetime as datetime
from datetime import timezone as timezone
from pathlib import Path as Path
from typing import Literal as Literal
from urllib.parse import urlparse as urlparse
from uuid import uuid4 as uuid4

import httpx as httpx

from app.core.path_utils import get_gateway_root as get_gateway_root
from app.gateway.control.gateway_state import GatewayStateStore as GatewayStateStore
from app.gateway.credentials import (
    FederationCredentialStore as FederationCredentialStore,
)
from app.gateway.federation import RemoteGatewayConnection as RemoteGatewayConnection
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeHealthProof as GatewayRuntimeHealthProof,
)
from app.gateway.runtime.workspace import WorkspaceRuntime as WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName as GatewayServiceName
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id as build_managed_local_workspace_id,
)
from app.gateway.workspace_ids import build_workspace_id as build_workspace_id
from app.gateway.workspace_ids import is_legacy_workspace_id as is_legacy_workspace_id
from app.schemas.gateway import (
    GatewayConfigReloadStatusDTO as GatewayConfigReloadStatusDTO,
)
from app.schemas.gateway import GatewayConnectionKind as GatewayConnectionKind
from app.schemas.gateway import (
    GatewayRemoteConnectionSummaryDTO as GatewayRemoteConnectionSummaryDTO,
)
from app.schemas.gateway import GatewayServiceStatus as GatewayServiceStatus
from app.schemas.gateway import GatewayServiceStatusDTO as GatewayServiceStatusDTO
from app.schemas.gateway import GatewayWorkspaceDTO as GatewayWorkspaceDTO
from app.services.infrastructure.config.state import (
    ConfigConflictError as ConfigConflictError,
)

from .core import _REGISTRY_SCHEMA_VERSION as _REGISTRY_SCHEMA_VERSION
from .core import _UNSET as _UNSET
from .core import GatewayRegistryBatchHandle as GatewayRegistryBatchHandle
from .core import RegistryCoreMixin
from .core import RegistryTargetOwner as RegistryTargetOwner
from .core import WorkspaceRouteLease as WorkspaceRouteLease
from .core import WorkspaceTarget as WorkspaceTarget
from .crud import RegistryCrudMixin
from .dtos import RegistryDtosMixin
from .persistence import RegistryPersistenceMixin
from .projection import RegistryProjectionMixin
from .remote import RegistryRemoteMixin
from .routes import RegistryRoutesMixin
from .runtime import RegistryRuntimeMixin

"""Gateway 工作区注册表（facade）。

原单文件 app/gateway/registry.py 已按垂直链路拆入本包：core.py 承载顶层常量、
数据类与 __init__/核心属性；persistence/crud/routes/runtime/remote/projection/dtos
七个 mixin 承载各条垂直链路；facade 组装并再导出全部原顶层符号（含原模块级
导入名），导入路径 app.gateway.registry 与其属性访问契约保持不变。
"""


class GatewayWorkspaceRegistry(
    RegistryCrudMixin,
    RegistryDtosMixin,
    RegistryPersistenceMixin,
    RegistryProjectionMixin,
    RegistryRemoteMixin,
    RegistryRoutesMixin,
    RegistryRuntimeMixin,
    RegistryCoreMixin,
):
    """Gateway 多工作区目标注册表：持久化、路由、运行时与 remote projection。"""
