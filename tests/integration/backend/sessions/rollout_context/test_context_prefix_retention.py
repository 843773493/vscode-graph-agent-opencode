"""真实来源 root 重建的历史保留语义与遍历成本。"""

import json
import sys
from pathlib import Path

import pytest

from app.domain.itemized.epoch.prefix_epoch import AppendedItemRef, EpochReason
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime import prefix_compiler
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_runtime_notice_consumption import (
    notice_case as notice_case,  # noqa: PLC0414 - 按当前文件隔离正式工作区
)


@pytest.mark.parametrize("source_count", [1, 8])
@pytest.mark.parametrize("revisions", [(2,), (2, 1, 3)])
def test_root_rebuild_visits_retained_source_refs_linearly(
    notice_case, monkeypatch, request, source_count, revisions,
):
    case = notice_case
    internal, _intent = case.internal("retained-sources")
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(case.session, case.thread), control_state_port=case.saver)
    expected = {}
    for index in range(source_count):
        name = f"retained-{index}"
        manager.register(ContextSourceDescriptor(
            source_id=name, source_kind="skill", name=name,
            description="保留来源装具", internal_locator=f"runtime:{name}", root_placement="root_eligible",
        ), tracking_status="tracked")
        expected[name] = f"冻结正文 {name}\n"
        manager.observe(name, expected[name], revision="retained:1")
    first = case.prepare(internal, "retained-initial", manager=manager)
    assert {source.source_identity for source in first.seal_binding.root.sources} == set(expected)
    for revision in revisions:
        for name in expected:
            body = expected[name] if revision == 1 else f"后续正文 {revision} {name}\n"
            manager.observe(name, body, revision=f"retained:{revision}")
        activated = case.prepare(internal, f"retained-updated:{revision}", manager=manager)
    before = canonical_json_bytes(activated.to_dict())
    for name in expected:
        assert manager.load_skill(name, mode="untrack").tracked is False
    _accepted, user = case.user("retained-user")
    # 用户入口消费 notice 并重建 epoch；重启确保来源来自已封存 detail。
    case.restart()
    reads = 0
    original_attribute = AppendedItemRef.__getattribute__

    def observe_attribute(ref, name):
        nonlocal reads
        # 只计编译器自身的身份访问；不替换 prefix、来源事实或任何计算结果。
        if name == "source_identity" and sys._getframe(1).f_code.co_filename == prefix_compiler.__file__:
            reads += 1
        return original_attribute(ref, name)

    monkeypatch.setattr(AppendedItemRef, "__getattribute__", observe_attribute)
    promoted = case.prepare(user, "retained-user-rebuilt")
    assert promoted.seal_binding.prefix.state.epoch.reason == EpochReason.RUNTIME_NOTICE_CONSUMED
    assert {
        source["source_identity"]: source["body"]
        for source in json.loads(promoted.seal_binding.root.content)["sources"]
    } == expected

    def tail_sources(snapshot):
        return tuple((ref.source_identity, ref.source_revision, block.body)
            for frame in snapshot.seal_binding.prefix.state.frames if frame.role == "user" and frame.source_protocol is not None
            for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True))

    # root 仅覆盖旧 revision；后续更新必须完整保留，不能按来源 ID 一刀切。
    assert tail_sources(promoted) == tuple(
        row for row in tail_sources(activated) if row[1] == f"retained:{revisions[-1]}"
    )
    assert {identity for identity, _revision, _body in tail_sources(promoted)} == set(expected)
    for snapshot, frozen in ((activated, before), (promoted, canonical_json_bytes(promoted.to_dict()))):
        assert canonical_json_bytes(case.saver.get_context_assembly(
            case.session, case.thread, assembly_id=snapshot.assembly_id,
        ).to_dict()) == frozen
    retained_refs = sum(len(frame.item_refs) for frame in activated.seal_binding.prefix.state.frames)
    measurement = {"sources": source_count, "revisions": revisions, "retained_refs": retained_refs, "identity_reads": reads}
    print(f"retained root lookup work: {measurement}")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"retained-root-{source_count}-{'-'.join(map(str, revisions))}.json").write_text(
        json.dumps(measurement, indent=2), encoding="utf-8",
    )
    assert reads <= 5 * retained_refs
