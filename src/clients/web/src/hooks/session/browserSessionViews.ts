import { browserGatewayId } from "../settings/browserUiSettings";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import type { SessionSelection, SessionViewState } from "../../state/sessionViewState";
import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../api/workspaceIdentity";

export async function readSessionView(port: number, workspaceId: string, sessionId: string, threadId?: string): Promise<SessionViewState | null> {
  const key = browserViewKey(await browserGatewayId(port), "session", workspaceId, sessionId, threadId ?? "main");
  const view = await readBrowserView<SessionViewState>(key);
  if (view && gatewayWorkspaceKey(view) !== workspaceId) throw new Error("浏览器视图记录的 Gateway／UUID 归属不匹配");
  return view ?? null;
}

export async function rememberSessionSelection(port: number, selection: SessionSelection | null): Promise<void> {
  await updateBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"), () => selection ?? undefined);
}

export async function readLatestSessionView(port: number): Promise<SessionViewState | null> {
  const selection = await readBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"));
  if (!selection) return null;
  return await readSessionView(port, gatewayWorkspaceKey(selection), selection.session_id) ?? {
    ...selection, turn_anchor: null, scroll_offset: 0, follow_latest: true,
    projection_version: 1, tool_details_expanded: false, updated_at: "",
  };
}

export async function writeSessionView(port: number, workspaceId: string, sessionId: string, payload: Omit<SessionViewState, "gateway_id" | "workspace_id" | "session_id" | "updated_at">, threadId?: string): Promise<SessionViewState> {
  const gatewayId = await browserGatewayId(port);
  const owner = workspaceOwnerFromKey(workspaceId);
  const value: SessionViewState = { ...payload, gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, session_id: sessionId, updated_at: new Date().toISOString() };
  await updateBrowserView(browserViewKey(gatewayId, "session", workspaceId, sessionId, threadId ?? "main"), () => value);
  return value;
}
