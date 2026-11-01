import "fake-indexeddb/auto";
import { expect, spyOn, test } from "bun:test";
import * as gateway from "../../gatewayApi";
import { readLatestSessionView, rememberSessionSelection, writeSessionView } from "./browserSessionViews";

test("点击会话即保存最近选择，后台滚动不改变选择，清空后不再恢复已删除会话", async () => {
  const health = spyOn(gateway, "getGatewayHealth").mockResolvedValue({ gateway_id: "gateway-selection" } as Awaited<ReturnType<typeof gateway.getGatewayHealth>>);
  try {
    await rememberSessionSelection(49695, { workspace_id: "workspace", session_id: "session-a" });
    const first = await readLatestSessionView(49695);
    expect(first?.session_id).toBe("session-a");
    expect(first?.follow_latest).toBe(true);
    await writeSessionView(49695, "workspace", "session-b", { turn_anchor: "turn-b", scroll_offset: 75,
      follow_latest: false, tool_details_expanded: true, projection_version: 1 });
    expect((await readLatestSessionView(49695))?.session_id).toBe("session-a");
    await rememberSessionSelection(49695, { workspace_id: "workspace", session_id: "session-b" });
    const second = await readLatestSessionView(49695);
    expect(second?.turn_anchor).toBe("turn-b"); expect(second?.scroll_offset).toBe(75);
    await rememberSessionSelection(49695, null);
    expect(await readLatestSessionView(49695)).toBeNull();
  } finally { health.mockRestore(); }
});
