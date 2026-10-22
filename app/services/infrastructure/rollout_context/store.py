"""rollout context 的唯一组合根；调用方显式选择持久化能力 owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from app.services.infrastructure.rollout_context.activation.schema import (
    create_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.assembly.store import (
    ContextAssemblyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.durable.store import (
    RolloutCheckpointStore,
)
from app.services.infrastructure.rollout_context.checkpoint.projection.attachments import (
    RolloutAttachmentProjection,
)
from app.services.infrastructure.rollout_context.execution.store import (
    RolloutExecutionStore,
)
from app.services.infrastructure.rollout_context.fork.store import RolloutForkStore
from app.services.infrastructure.rollout_context.operations.pruning_owner import (
    RolloutPruningOwner,
)
from app.services.infrastructure.rollout_context.storage.primitives import MessageCodec
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutStorage,
    SerializerPort,
)


class RolloutContextStore:
    def __init__(
        self,
        sessions_dir: str | Path,
        *,
        serde: SerializerPort | None = None,
        message_codec: MessageCodec | None = None,
        storage: RolloutStorage | None = None,
        fork_store_factory: Callable[[RolloutContextStore], RolloutForkStore] = RolloutForkStore,
        attachment_ref_projector: Callable[[str, str, str], dict[str, str]] | None = None,
    ) -> None:
        def recover_materialization(
            session_id: str,
            thread_id: str,
            checkpoint_ns: str,
            connection: sqlite3.Connection,
            jsonl_path: Path,
        ) -> None:
            self.forks._recover_fork_materialization(
                session_id, thread_id, checkpoint_ns, connection, jsonl_path,
            )

        self.storage = storage or RolloutStorage(
            sessions_dir, serde=serde, message_codec=message_codec,
            fork_recovery=recover_materialization, initialize_activation=create_resource_activation_schema,
        )
        if storage is not None:
            storage._fork_recovery = recover_materialization
        self.attachment_projection = RolloutAttachmentProjection(attachment_ref_projector)
        self.executions = RolloutExecutionStore(self)
        self.checkpoints = RolloutCheckpointStore(self)
        self.assemblies = ContextAssemblyStore(self)
        self.forks = fork_store_factory(self)
        self.pruning = RolloutPruningOwner(self.storage)
