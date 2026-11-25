import * as sessions from "../../api/session/sessions";
import { afterEach } from "bun:test";
import { nodeKey, disclosureKey } from "../../state/timeline/transcriptNodes";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import "fake-indexeddb/auto";
import { expect, spyOn, test } from "bun:test";
import * as gateway from "../../gatewayApi";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { readLatestSessionView, readSessionView, rememberSessionSelection, writeSessionView } from "./browserSessionViews";

const mainThread = spyOn(sessions, "getSessionMainThread").mockResolvedValue({thread_id:"thread-main"} as Awaited<ReturnType<typeof sessions.getSessionMainThread>>);
afterEach(() => mainThread.mockClear());
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
    await writeSessionView(49695, WORKSPACE_KEY, "session-b", { reading_anchor: {node_key:nodeKey({kind:"turn",id:"turn-b"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:75}, disclosures:{},
      follow_latest: false, tool_details_expanded: true, projection_version: 3, thread_id:"thread-main" }, "thread-main");
    expect((await readLatestSessionView(49695))?.session_id).toBe("session-a");
    await rememberSessionSelection(49695, { ...OWNER, session_id: "session-b" });
    const second = await readLatestSessionView(49695);
    expect(second?.reading_anchor?.node_key.source_ref.id).toBe("turn-b"); expect(second?.reading_anchor?.viewport_offset_px).toBe(75);
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
      reading_anchor: {node_key:nodeKey({kind:"turn",id:"turn-local"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:10}, disclosures:{}, follow_latest: false,
      tool_details_expanded: false, projection_version: 3, thread_id:"thread-main",
    }, "thread-main");
    await writeSessionView(49696, otherKey, "session-shared", {
      reading_anchor: {node_key:nodeKey({kind:"turn",id:"turn-remote"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:25}, disclosures:{}, follow_latest: false,
      tool_details_expanded: true, projection_version: 3, thread_id:"thread-main",
    }, "thread-main");

    const local = await readSessionView(49696, WORKSPACE_KEY, "session-shared");
    const remote = await readSessionView(49696, otherKey, "session-shared");
    expect(local).toMatchObject({ ...OWNER, reading_anchor: {node_key:nodeKey({kind:"turn",id:"turn-local"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:10}, disclosures:{} });
    expect(remote).toMatchObject({ ...otherOwner, reading_anchor: {node_key:nodeKey({kind:"turn",id:"turn-remote"},"header"),content_position:{block:0,offset_px:0},viewport_offset_px:25}, disclosures:{} });
    await rememberSessionSelection(49696, { ...otherOwner, session_id: "session-shared" });
    expect(await readLatestSessionView(49696)).toEqual(remote);
  } finally { health.mockRestore(); }
});

test("Item 的 slot 阅读位置和折叠分别往返，旧格式拒绝恢复并保留原件与其它记录", async () => {
  const gatewayId = "gateway-reading-v3";
  const health = spyOn(gateway, "getGatewayHealth").mockResolvedValue({gateway_id:gatewayId} as Awaited<ReturnType<typeof gateway.getGatewayHealth>>);
  const body = nodeKey({kind:"item",id:"same-item"},"body");
  const summary = nodeKey({kind:"item",id:"same-item"},"summary");
  const oldKey = browserViewKey(gatewayId,"session",WORKSPACE_KEY,"old-view","thread-main");
  const oldView = {...OWNER, session_id:"old-view",thread_id:"thread-main",projection_version:2,
    reading_anchor:{node_key:{source_ref:{kind:"item",id:"same-item",part:1},slot:"body"},
      content_position:{block:5,offset_px:12},viewport_offset_px:24},
    disclosures:{},follow_latest:false,tool_details_expanded:false,updated_at:"2026-11-23T00:00:00Z"};
  try {
    await updateBrowserView(oldKey,()=>oldView);
    const saved = await writeSessionView(49697,WORKSPACE_KEY,"new-view",{
      thread_id:"thread-main",projection_version:3,follow_latest:false,tool_details_expanded:false,
      reading_anchor:{node_key:summary,content_position:{block:5,offset_px:12},viewport_offset_px:24},
      disclosures:{[disclosureKey(body)]:true,[disclosureKey(summary)]:false},
    },"thread-main");
    expect(await readSessionView(49697,WORKSPACE_KEY,"new-view","thread-main")).toEqual(saved);
    await expect(readSessionView(49697,WORKSPACE_KEY,"old-view","thread-main")).rejects.toThrow("原记录已保留");
    expect(await readBrowserView<typeof oldView>(oldKey)).toEqual(oldView);
    // 不能把旧 Part 坐标只改版本号就当作当前格式。
    await updateBrowserView(oldKey,()=>({...oldView,projection_version:3}));
    await expect(readSessionView(49697,WORKSPACE_KEY,"old-view","thread-main")).rejects.toThrow("原记录已保留");
    expect(await readSessionView(49697,WORKSPACE_KEY,"new-view","thread-main")).toEqual(saved);
  } finally { health.mockRestore(); }
});
