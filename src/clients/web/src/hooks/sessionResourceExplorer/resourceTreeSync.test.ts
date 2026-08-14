import { describe, expect, test } from "bun:test";
import type { GatewayWorkspace, Session } from "../../types/backend";
import {
  buildSessionCatalogSyncKeys,
  buildWorkspaceNavigationSyncKey,
  changedCatalogWorkspaceIds,
} from "./resourceTreeSync";

function session(
  sessionId: string,
  title: string,
  parentSessionId: string | null = null,
): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws-1",
    title,
    title_source: "user",
    current_agent_id: "agent",
    current_provider_id: null,
    parent_session_id: parentSessionId,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("resourceTreeSync", () => {
  test("工作区状态变化不刷新导航，增删改名会刷新", () => {
    const ready = ([
      { workspace_id: "ws-1", name: "工作区", status: "ready" },
    ] as GatewayWorkspace[]);
    const offline = [{ ...ready[0], status: "offline" }] as GatewayWorkspace[];
    const renamed = [{ ...ready[0], name: "新名称" }] as GatewayWorkspace[];

    expect(buildWorkspaceNavigationSyncKey(ready)).toBe(
      buildWorkspaceNavigationSyncKey(offline),
    );
    expect(buildWorkspaceNavigationSyncKey(ready)).not.toBe(
      buildWorkspaceNavigationSyncKey(renamed),
    );
  });

  test("会话标题、父子关系和成员变化会改变目录同步键", () => {
    const initial = buildSessionCatalogSyncKeys(new Map([
      ["ws-1", [session("s-1", "旧标题")]],
    ]));
    const renamed = buildSessionCatalogSyncKeys(new Map([
      ["ws-1", [session("s-1", "新标题")]],
    ]));
    const moved = buildSessionCatalogSyncKeys(new Map([
      ["ws-1", [session("s-1", "旧标题", "s-parent")]],
    ]));

    expect(initial.get("ws-1")).not.toBe(renamed.get("ws-1"));
    expect(initial.get("ws-1")).not.toBe(moved.get("ws-1"));
  });

  test("合并后端镜像变化和显式目录失效请求", () => {
    const changed = changedCatalogWorkspaceIds(
      new Map([["ws-1", "a"], ["ws-2", "b"]]),
      new Map([["ws-1", "c"], ["ws-2", "b"]]),
      new Map([["ws-1", 1], ["ws-2", 1]]),
      new Map([["ws-1", 1], ["ws-2", 2]]),
    );

    expect(new Set(changed)).toEqual(new Set(["ws-1", "ws-2"]));
  });
});
