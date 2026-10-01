"""Gateway 配置加载与热重载（facade）。

原单文件 app/gateway/config.py 已按垂直链路拆入本包：values.py 承载配置值对象与
取值/路径解析；connection_ids.py 承载 connection_id 规范化与迁移；sources.py 承载
来源层加载/迁移/日志；loader.py 承载 load_gateway_config 与 consumer 健康摘要；
reload_lifecycle.py 与 reload_pending_restart.py 承载 GatewayConfigReloadService 的
两个 mixin。本 facade 组装并再导出全部原顶层符号（含原模块级导入名），导入路径
app.gateway.config 与其属性访问契约保持不变。
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import shutil as shutil
import tempfile as tempfile
from collections.abc import Awaitable as Awaitable
from collections.abc import Callable as Callable
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from dataclasses import replace as replace
from datetime import datetime as datetime
from datetime import timezone as timezone
from pathlib import Path as Path
from typing import Literal as Literal
from typing import cast as cast

from app.core.config_sources import ConfigSource as ConfigSource
from app.core.config_sources import ConfigSourceLayer as ConfigSourceLayer
from app.core.config_sources import config_revision as config_revision
from app.core.config_sources import parse_stable_config_file as parse_stable_config_file
from app.core.config_sources import read_stable_config_file as read_stable_config_file
from app.core.config_sources import (
    verify_stable_config_file as verify_stable_config_file,
)
from app.core.history_loading import (
    DEFAULT_ANCHOR_AFTER_TURNS as DEFAULT_ANCHOR_AFTER_TURNS,
)
from app.core.history_loading import (
    DEFAULT_ANCHOR_BEFORE_TURNS as DEFAULT_ANCHOR_BEFORE_TURNS,
)
from app.core.history_loading import DEFAULT_ANCHOR_INCLUDE as DEFAULT_ANCHOR_INCLUDE
from app.core.history_loading import DEFAULT_INITIAL_INCLUDE as DEFAULT_INITIAL_INCLUDE
from app.core.history_loading import DEFAULT_INITIAL_TURNS as DEFAULT_INITIAL_TURNS
from app.core.history_loading import HistoryLoadingConfig as HistoryLoadingConfig
from app.core.path_utils import get_user_config_root as get_user_config_root
from app.core.path_utils import (
    get_user_gateway_config_path as get_user_gateway_config_path,
)
from app.core.path_utils import (
    get_user_gateway_local_config_path as get_user_gateway_local_config_path,
)
from app.core.path_utils import (
    get_user_gateway_schema_path as get_user_gateway_schema_path,
)
from app.gateway.control.gateway_state import GatewayStateStore as GatewayStateStore
from app.services.infrastructure.config import ConfigFileWatcher as ConfigFileWatcher
from app.services.infrastructure.config import ConfigReloadStatus as ConfigReloadStatus
from app.services.infrastructure.config.policy import (
    gateway_config_policy as gateway_config_policy,
)
from app.services.infrastructure.config.source_vrn import (
    inline_config_source_vrn as inline_config_source_vrn,
)
from app.services.infrastructure.config.state import (
    ConfigConflictError as ConfigConflictError,
)
from app.services.infrastructure.config.state import (
    ConfigEventInput as ConfigEventInput,
)
from app.services.infrastructure.config.state import (
    SecretReferenceRequiredError as SecretReferenceRequiredError,
)
from app.services.infrastructure.config.state import (
    build_secret_binding_summary as build_secret_binding_summary,
)
from app.services.infrastructure.config.state import (
    changed_json_paths as changed_json_paths,
)
from app.services.infrastructure.config.state import dump_json as dump_json
from app.services.infrastructure.config.state import new_config_id as new_config_id
from app.services.infrastructure.config.state import (
    prepare_config_for_persistence as prepare_config_for_persistence,
)
from app.services.infrastructure.config.state import (
    redact_config_payload as redact_config_payload,
)
from configs.installer import (
    resolve_config_resource_source as resolve_config_resource_source,
)
from configs.runtime import merge_json_objects as merge_json_objects
from configs.runtime import read_jsonc_object as read_jsonc_object
from configs.runtime import validate_config as validate_config

from .connection_ids import _atomic_write_gateway_jsonc as _atomic_write_gateway_jsonc
from .connection_ids import (
    _connection_identity_fingerprint as _connection_identity_fingerprint,
)
from .connection_ids import (
    _jsonc_workspaces_object_starts as _jsonc_workspaces_object_starts,
)
from .connection_ids import (
    _migrate_gateway_connection_ids_in_source as _migrate_gateway_connection_ids_in_source,
)
from .connection_ids import _new_connection_id as _new_connection_id
from .connection_ids import _normalize_connection_ids as _normalize_connection_ids
from .connection_ids import (
    rollback_gateway_connection_id_migration as rollback_gateway_connection_id_migration,
)
from .loader import (
    _consumer_health_digests_from_proof as _consumer_health_digests_from_proof,
)
from .loader import (
    _normalize_consumer_health_digests as _normalize_consumer_health_digests,
)
from .loader import (
    _require_gateway_consumer_health_digests as _require_gateway_consumer_health_digests,
)
from .loader import load_gateway_config as load_gateway_config
from .reload_lifecycle import ReloadLifecycleMixin
from .reload_pending_restart import ReloadPendingRestartMixin
from .sources import _gateway_source_detail as _gateway_source_detail
from .sources import (
    _load_or_migrate_gateway_override as _load_or_migrate_gateway_override,
)
from .sources import _record_gateway_source_journal as _record_gateway_source_journal
from .sources import (
    record_gateway_restart_startup_failure as record_gateway_restart_startup_failure,
)
from .values import _GATEWAY_SOURCE_LAYER_AUTHORITY as _GATEWAY_SOURCE_LAYER_AUTHORITY
from .values import (
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS as REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
)
from .values import ConfiguredRemoteGateway as ConfiguredRemoteGateway
from .values import ConfiguredTheme as ConfiguredTheme
from .values import GatewayConfig as GatewayConfig
from .values import GatewayConfigRuntimeApplier as GatewayConfigRuntimeApplier
from .values import GatewayConfigRuntimeRollback as GatewayConfigRuntimeRollback
from .values import GatewayHistoryLoadingConfig as GatewayHistoryLoadingConfig
from .values import _configured_theme_background as _configured_theme_background
from .values import _history_loading_config as _history_loading_config
from .values import _nested_config_value as _nested_config_value
from .values import _positive_integer_config as _positive_integer_config
from .values import _positive_number_config as _positive_number_config
from .values import _skill_groups_config as _skill_groups_config
from .values import _workspace_from_validated_config as _workspace_from_validated_config
from .values import resolve_gateway_path as resolve_gateway_path


class GatewayConfigReloadService(
    ReloadLifecycleMixin,
    ReloadPendingRestartMixin,
):
    """Gateway 配置的候选、pending 和 active 管理器。"""
