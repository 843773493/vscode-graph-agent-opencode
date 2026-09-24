"""扩展 dispatch binding 与 plan/request/ToolSetRef hash 的边界合同测试（9.x 扩展验收）。

锁定三条互斥边界：

* ``extension_dispatch_binding_hash``（及其 catalog binding hash、目标身份）
  不进入 ``context-plan-hash:v2`` 的 canonical preimage；
* 不进入 Provider ``ToolSetRef`` 的 manifest content_hash；
* 不进入仅描述 Provider wire bytes 的 ``request_hash``。

证明方式：探针 monkeypatch plan/request hash 使用的唯一 ``sha256_jcs``，捕获真实
canonical preimage 与 digest，再断言扩展 binding 身份既不出现在 preimage 中、也
不等于任一 digest。反向同时成立：Provider 可见直接工具变化会改变 plan/request
hash，但不会改变已封存的 dispatch binding hash。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.mcp.extension_catalog import (
    EXTENSION_TOOL_ENVELOPE_IDENTITY,
    ExtensionDispatchBindingRef,
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
)

SESSION_ID = "ses_boundary"
PLAN_ID = "plan_boundary"
ASSEMBLY_ID = "assembly_boundary"
TARGET_ID = "mcp__mini__echo"


def _direct_tools(extra: str | None = None) -> list[dict[str, object]]:
    """Provider 可见直接工具 + 固定信封；扩展目标目录不进入其中。"""
    tools: list[dict[str, object]] = [
        {"tool_id": "read_file", "description": "读取"},
        {
            "tool_id": EXTENSION_TOOL_ENVELOPE_IDENTITY.split("@", 1)[0],
            "description": EXTENSION_TOOL_ENVELOPE_IDENTITY,
        },
    ]
    if extra is not None:
        tools.append({"tool_id": extra, "description": "新增直接工具"})
    return tools


def _plan(tool_snapshot: list[dict[str, object]]) -> ContextRequestPlan:
    manifest_ref = ToolSetRef.from_tool_snapshot(
        snapshot_id="tool-set:boundary",
        session_id=SESSION_ID,
        plan_id=PLAN_ID,
        tools=tool_snapshot,
        source_revision="tool-rev:boundary",
    )
    bound_ref = replace(manifest_ref, assembly_id=ASSEMBLY_ID)
    selection = (
        ContextSelectionEntry(
            assembly_id=ASSEMBLY_ID,
            plan_ordinal=0,
            ref=bound_ref,
            selection_kind="tool_set",
            source_revision=bound_ref.source_revision,
            content_length=bound_ref.content_length,
            content_hash=bound_ref.content_hash,
        ),
    )
    return ContextRequestPlan(
        session_id=SESSION_ID,
        plan_id=PLAN_ID,
        refs=(),
        tool_set_refs=(manifest_ref,),
        active_view_id="view:boundary",
        history_view_revision=1,
    ).seal_for_assembly(ASSEMBLY_ID, selection=selection)


def _dispatch(
    *,
    catalog_revision: str,
    guidance_revision: str,
    policy_revision: str = "resource-activation-policy:v1:test",
    generation: int = 1,
) -> ExtensionDispatchBindingRef:
    binding = build_extension_catalog_binding(
        catalog_revision=catalog_revision,
        generation=generation,
        targets=[
            ExtensionTargetBindingInput(
                target_id=TARGET_ID,
                origin="mcp",
                server_id="mini",
                args={"text": {"type": "string"}},
            )
        ],
    )
    return ExtensionDispatchBindingRef(
        binding_ref=binding,
        guidance_revision=guidance_revision,
        activation_policy_revision=policy_revision,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn:boundary",
    )


def _captured_preimages(monkeypatch: pytest.MonkeyPatch, plan: ContextRequestPlan):
    """捕获 plan hash 与 request hash 的真实 canonical preimage 与 digest。"""
    from app.domain.itemized import plan_hash as plan_hash_module
    from app.domain.itemized import request_hash as request_hash_module

    captured: dict[str, list[object]] = {"payloads": [], "digests": []}

    def _record(value: object) -> str:
        captured["payloads"].append(value)
        digest = sha256_jcs(value)
        captured["digests"].append(digest)
        return digest

    monkeypatch.setattr(plan_hash_module, "sha256_jcs", _record)
    monkeypatch.setattr(request_hash_module, "sha256_jcs", _record)
    plan_digest = plan.plan_hash()
    request_digest = request_hash_module.context_request_hash(plan, "provider-x")
    return plan_digest, request_digest, captured


def test_dispatch_identity_absent_from_plan_and_request_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(_direct_tools())
    dispatch = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "b" * 64,
    )
    plan_digest, request_digest, captured = _captured_preimages(monkeypatch, plan)
    assert captured["payloads"], "探针必须捕获到 plan/request preimage"

    rendered = "\n".join(canonical_json_bytes(payload).decode() for payload in captured["payloads"])
    # 扩展 binding 身份不进入任何 preimage。
    assert dispatch.dispatch_binding_hash not in rendered
    assert dispatch.binding_ref.binding_hash not in rendered
    assert dispatch.binding_ref.catalog_revision not in rendered
    assert dispatch.guidance_revision not in rendered
    assert dispatch.activation_policy_revision not in rendered
    assert dispatch.binding_ref.binding_id not in rendered
    # 独立 hash 不等于 plan/request hash。
    assert dispatch.dispatch_binding_hash != plan_digest
    assert dispatch.dispatch_binding_hash != request_digest


def test_dispatch_hash_differs_when_catalog_or_guidance_changes() -> None:
    """catalog semantic revision / 指引 / 策略变化必须改变独立 dispatch hash。"""
    base = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "b" * 64,
    )
    catalog_changed = _dispatch(
        catalog_revision="sha256:" + "c" * 64,
        guidance_revision="sha256:" + "b" * 64,
    )
    guidance_changed = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "d" * 64,
    )
    policy_changed = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "b" * 64,
        policy_revision="resource-activation-policy:v1:other",
    )
    hashes = {
        base.dispatch_binding_hash,
        catalog_changed.dispatch_binding_hash,
        guidance_changed.dispatch_binding_hash,
        policy_changed.dispatch_binding_hash,
    }
    assert len(hashes) == 4
    # 运行 identity 不进入内容 hash。
    identity_changed = ExtensionDispatchBindingRef(
        binding_ref=base.binding_ref,
        guidance_revision=base.guidance_revision,
        activation_policy_revision=base.activation_policy_revision,
        owner_session_id="ses_other",
        owner_thread_id="other",
        turn_id="turn:other",
    )
    assert identity_changed.dispatch_binding_hash == base.dispatch_binding_hash


def test_toolset_manifest_hash_has_no_extension_identity() -> None:
    """ToolSetRef manifest 只含 Provider 可见直接工具 + 固定信封。"""
    plan = _plan(_direct_tools())
    tool_ref = plan.selection[0].ref
    assert isinstance(tool_ref, ToolSetRef)
    manifest_preimage = tool_ref.manifest_preimage()
    tool_ids = {str(tool["tool_id"]) for tool in manifest_preimage["tools"]}
    assert tool_ids == {"read_file", "invoke_extension_tool"}
    # manifest hash 可独立重算且只覆盖 manifest preimage。
    assert tool_ref.content_hash == sha256_jcs(manifest_preimage)
    serialized = canonical_json_bytes(manifest_preimage).decode()
    assert TARGET_ID not in serialized
    assert "extension-catalog-binding" not in serialized
    assert "extension-dispatch-binding" not in serialized


def test_direct_tool_change_moves_plan_hash_but_not_dispatch_hash() -> None:
    """Provider wire prefix 变化改变 plan/request hash，但封存 dispatch hash 不漂移。"""
    baseline = _plan(_direct_tools())
    extended = _plan(_direct_tools(extra="write_file"))
    assert baseline.plan_hash() != extended.plan_hash()
    dispatch = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "b" * 64,
    )
    sealed_hash = dispatch.dispatch_binding_hash
    assert dispatch.verify().dispatch_binding_hash == sealed_hash


def test_inner_catalog_change_does_not_move_plan_hash() -> None:
    """内层目录变化（新 target）不制造 ToolSetRef/plan hash 漂移。"""
    plan = _plan(_direct_tools())
    plan_hash = plan.plan_hash()
    first = _dispatch(
        catalog_revision="sha256:" + "a" * 64,
        guidance_revision="sha256:" + "b" * 64,
        generation=1,
    )
    second = _dispatch(
        catalog_revision="sha256:" + "c" * 64,
        guidance_revision="sha256:" + "b" * 64,
        generation=2,
    )
    assert first.dispatch_binding_hash != second.dispatch_binding_hash
    # plan hash 与 Provider ToolSetRef 完全不受内层目录变化影响。
    assert plan.plan_hash() == plan_hash
    tool_ref = plan.selection[0].ref
    assert isinstance(tool_ref, ToolSetRef)
    assert tool_ref.content_hash == sha256_jcs(tool_ref.manifest_preimage())
