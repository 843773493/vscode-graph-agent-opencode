import { readableAnchor } from "../../state/timeline/transcriptNodes";
import { getSessionMainThread } from "../../api/session/sessions";
import { browserGatewayId } from "../settings/browserUiSettings";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import type { SessionSelection, SessionViewState } from "../../state/sessionViewState";
import { SESSION_VIEW_PROJECTION_VERSION } from "../../state/sessionViewState";
import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../api/workspaceIdentity";

export async function readSessionView(port: number, workspaceId: string, sessionId: string, threadId?: string): Promise<SessionViewState | null> {
  const actualThread = threadId ?? (await getSessionMainThread(port, sessionId, {gateway_id:workspaceOwnerFromKey(workspaceId).gatewayId,workspace_id:workspaceOwnerFromKey(workspaceId).workspaceId})).thread_id;
  const key = browserViewKey(await browserGatewayId(port), "session", workspaceId, sessionId, actualThread);
  const view = await readBrowserView<SessionViewState>(key);
  if (view && gatewayWorkspaceKey(view) !== workspaceId) throw new Error("浏览器视图记录的 Gateway／UUID 归属不匹配");
  if (view && (view.projection_version !== SESSION_VIEW_PROJECTION_VERSION || view.thread_id !== actualThread || view.reading_anchor !== null && !readableAnchor(view.reading_anchor)
    || typeof view.follow_latest !== "boolean" || !view.disclosures || typeof view.disclosures !== "object" || Array.isArray(view.disclosures)
    || Object.values(view.disclosures).some(value => typeof value !== "boolean"))) {
    throw new Error("浏览器阅读记录格式不支持，原记录已保留，未恢复阅读位置");
  }
  return view ?? null;
}

export async function rememberSessionSelection(port: number, selection: SessionSelection | null): Promise<void> {
  await updateBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"), () => selection ?? undefined);
}

export async function readLatestSessionView(port: number): Promise<SessionViewState | null> {
  const selection = await readBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"));
  if (!selection) return null;
  const thread = await getSessionMainThread(port, selection.session_id, selection);
  return await readSessionView(port, gatewayWorkspaceKey(selection), selection.session_id, thread.thread_id) ?? {
    ...selection, thread_id: thread.thread_id, reading_anchor: null, disclosures: {}, follow_latest: true,
    projection_version: SESSION_VIEW_PROJECTION_VERSION, tool_details_expanded: false, updated_at: "",
  };
}

export async function writeSessionView(port: number, workspaceId: string, sessionId: string, payload: Omit<SessionViewState, "gateway_id" | "workspace_id" | "session_id" | "updated_at">, threadId?: string): Promise<SessionViewState> {
  if (!threadId || payload.thread_id !== threadId) throw new Error("保存阅读位置需要真实 Thread");
  const gatewayId = await browserGatewayId(port);
  const owner = workspaceOwnerFromKey(workspaceId);
  const value: SessionViewState = { ...payload, gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, session_id: sessionId, updated_at: new Date().toISOString() };
  await updateBrowserView(browserViewKey(gatewayId, "session", workspaceId, sessionId, threadId), () => value);
  return value;
}
