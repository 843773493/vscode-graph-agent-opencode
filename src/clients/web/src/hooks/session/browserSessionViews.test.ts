import "fake-indexeddb/auto";
import { expect, spyOn, test } from "bun:test";
import * as gateway from "../../gatewayApi";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { readLatestSessionView, readSessionView, rememberSessionSelection, writeSessionView } from "./browserSessionViews";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";
const OWNER = { gateway_id: "gateway-selection", workspace_id: WORKSPACE_ID };
const WORKSPACE_KEY = gatewayWorkspaceKey(OWNER);

test("点击会话即保存最近选择，后台滚动不改变选择，清空后不再恢复已删除会话", async () => {
  const health = spyOn(gateway, "getGatewayHealth").mockResolvedValue({ gateway_id: "gateway-selection" } as Awaited<ReturnType<typeof gateway.getGatewayHealth>>);
  try {
    await rememberSessionSelection(49695, { ...OWNER, session_id: "session-a" });
    const first = await readLatestSessionView(49695);
    expect(first?.session_id).toBe("session-a");
    expect(first?.gateway_id).toBe(OWNER.gateway_id);
    expect(first?.workspace_id).toBe(WORKSPACE_ID);
    expect(first?.follow_latest).toBe(true);
    await writeSessionView(49695, WORKSPACE_KEY, "session-b", { turn_anchor: "turn-b", scroll_offset: 75,
      follow_latest: false, tool_details_expanded: true, projection_version: 1 });
    expect((await readLatestSessionView(49695))?.session_id).toBe("session-a");
    await rememberSessionSelection(49695, { ...OWNER, session_id: "session-b" });
    const second = await readLatestSessionView(49695);
    expect(second?.turn_anchor).toBe("turn-b"); expect(second?.scroll_offset).toBe(75);
    await rememberSessionSelection(49695, null);
    expect(await readLatestSessionView(49695)).toBeNull();
  } finally { health.mockRestore(); }
});

test("相同 UUID 与会话在不同 Gateway 下的浏览器视图独立保存原生 owner", async () => {
  const health = spyOn(gateway, "getGatewayHealth").mockResolvedValue({ gateway_id: "gateway-view-isolation" } as Awaited<ReturnType<typeof gateway.getGatewayHealth>>);
  const otherOwner = { gateway_id: "gateway-other", workspace_id: WORKSPACE_ID };
  const otherKey = gatewayWorkspaceKey(otherOwner);
  try {
    await writeSessionView(49696, WORKSPACE_KEY, "session-shared", {
      turn_anchor: "turn-local", scroll_offset: 10, follow_latest: false,
      tool_details_expanded: false, projection_version: 1,
    });
    await writeSessionView(49696, otherKey, "session-shared", {
      turn_anchor: "turn-remote", scroll_offset: 25, follow_latest: false,
      tool_details_expanded: true, projection_version: 1,
    });

    const local = await readSessionView(49696, WORKSPACE_KEY, "session-shared");
    const remote = await readSessionView(49696, otherKey, "session-shared");
    expect(local).toMatchObject({ ...OWNER, turn_anchor: "turn-local", scroll_offset: 10 });
    expect(remote).toMatchObject({ ...otherOwner, turn_anchor: "turn-remote", scroll_offset: 25 });
    await rememberSessionSelection(49696, { ...otherOwner, session_id: "session-shared" });
    expect(await readLatestSessionView(49696)).toEqual(remote);
  } finally { health.mockRestore(); }
});
