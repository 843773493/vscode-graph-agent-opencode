"""通用导航通过正式 Gateway API、SSH channel 与目标文件 owner 的闭包。"""

from __future__ import annotations

import json
from uuid import uuid4

from app.core.resource_uri import resource_display_uri
from app.gateway.credentials import FederationCredentialStore
from tests.integration.backend.agents.test_resource_navigation import _tracking_counts
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - 正式 fixture 按当前测试文件隔离
)


async def test_navigation_uses_original_star_channels_and_fixed_owner(communication_triad) -> None:
    nodes, model, artifacts = communication_triad
    source, hub, target = nodes["b"], nodes["a"], nodes["c"]
    child = target.workspace / "src/联邦 文件.v2.md"
    child.write_text("跨 Gateway 正文 Ω\n")
    skill = target.workspace / ".boxteam/skills/navigation/references"
    skill.mkdir(parents=True)
    (skill.parent / "SKILL.md").write_text("---\nname: navigation\ndescription: 导航验证\n---\n正文\n")
    (skill / "远端.md").write_text("工作区技能 child\n")
    assert target.gateway_home is not None
    gateway_skill = target.gateway_home / "skills/navigation/scripts"
    gateway_skill.mkdir(parents=True)
    (gateway_skill.parent / "SKILL.md").write_text("---\nname: navigation\ndescription: 导航验证\n---\n正文\n")
    (gateway_skill / "run.py").write_text("print('远端')\n")
    before = _tracking_counts(target.workspace / ".boxteam")
    evidence = []

    def uri(kind: str, *tail: str, scope: str = "workspace", authority: str | None = target.gateway_id, workspace_id: str = target.workspace_id) -> str:
        return resource_display_uri(scope=scope, scope_id=workspace_id if scope == "workspace" else target.gateway_id, kind=kind, tail_segments=tail, gateway_authority=authority)

    async def navigate(node, address: str, operation: str, **extra: object) -> dict:
        response = await node.client.post("/api/v1/resources/navigation", json={"uri": address, "operation": operation, **extra})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        for member in nodes.values():
            assert str(member.workspace) not in response.text
            assert str(member.gateway_home) not in response.text
        data = response.json()["data"]
        assert data["node"]["uri"] == address
        evidence.append({"source": node.gateway_id, "uri": address, "operation": operation, "data": data})
        return data

    root = uri("files", "root", "src")
    # hub 一跳直达与 spoke 经唯一 hub 的两跳返回相同可继续访问的目标 URI。
    direct = await navigate(hub, root, "list")
    relayed = await navigate(source, root, "list")
    assert direct == relayed
    # canonical remote route 也必须使用同一 read grant；不能依赖对端 HTTP 凭据直读。
    local_root = uri("files", "root", "src", authority=None)
    canonical_path = f"/api/v1/workspaces/{target.workspace_id}/resources/navigation"
    canonical = await hub.client.post(canonical_path, json={"uri": local_root, "operation": "list"})
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["data"] == direct
    assert canonical.headers["X-Request-ID"] == canonical.json()["request_id"]
    credential = FederationCredentialStore(storage_path=hub.gateway_root / "credentials/federation.json").get("rgw_c3_c")
    for path in ("/api/v1/resources/navigation", canonical_path):
        rejected = await target.client.post(path, json={"uri": local_root, "operation": "list"}, headers={
            "X-Local-Token": "", "X-BoxTeam-Federation-Token": credential.token,
        })
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"] == "联邦资源导航必须通过原 read grant relay"
    entry = next(item for item in relayed["entries"] if item["uri"].endswith("%E8%81%94%E9%82%A6%20%E6%96%87%E4%BB%B6.v2.md"))
    assert entry["uri"].startswith(f"boxteam://{target.gateway_id}/")
    assert (await navigate(source, entry["uri"], "read"))["content"] == "跨 Gateway 正文 Ω\n"
    found = await navigate(source, root, "search", query="联邦", limit=1)
    assert len(found["entries"]) == 1 and found["entries"][0]["uri"] == entry["uri"]
    assert (await navigate(source, entry["uri"], "read", max_bytes=3))["truncated"] is True
    for scope, segments, expected in (
        ("workspace", ("navigation", "references", "远端.md"), "工作区技能 child\n"),
        ("gateway", ("navigation", "scripts", "run.py"), "print('远端')\n"),
    ):
        address = uri("skills", *segments, scope=scope)
        assert (await navigate(source, address, "read"))["content"] == expected
    # 缺省与本机 authority 同解，且不把 active workspace 当 URI 的补全来源。
    local = uri("files", "root", "src", child.name, authority=None)
    explicit = uri("files", "root", "src", child.name)
    local_data = await navigate(target, local, "read")
    same = await target.client.post("/api/v1/resources/navigation", json={"uri": explicit, "operation": "read"})
    assert same.status_code == 200 and same.json()["data"] == local_data
    missing_codes = []
    for address, code in (
        (uri("files", "root", "missing.txt"), "target_not_resolvable"),
        (uri("files", "root", workspace_id=str(uuid4())), "target_not_resolvable"),
        (uri("files", "root", ".boxteam", "workspace-identity.json"), "target_not_resolvable"),
        (uri("files", "root", authority="gateway_unknown"), "federation-unknown-peer"),
    ):
        response = await source.client.post("/api/v1/resources/navigation", json={"uri": address, "operation": "read"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == code
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert str(target.workspace) not in response.text
        missing_codes.append(response.json()["detail"])
    assert missing_codes[0] == missing_codes[1] == missing_codes[2]
    assert _tracking_counts(target.workspace / ".boxteam") == before
    assert not model.requests
    assert {(peer, direction) for peer, direction in source.channels()} == {(hub.gateway_id, "inbound")}
    assert {(peer, direction) for peer, direction in target.channels()} == {(hub.gateway_id, "inbound")}
    (artifacts / "navigation-federation.json").write_text(json.dumps({"requests": evidence, "rejected": missing_codes}, ensure_ascii=False, indent=2) + "\n")
