import { browserGatewayId } from "../settings/browserUiSettings";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import type { SessionSelection, SessionViewState } from "../../state/sessionViewState";

export async function readSessionView(port: number, workspaceId: string, sessionId: string, threadId?: string): Promise<SessionViewState | null> {
  const key = browserViewKey(await browserGatewayId(port), "session", workspaceId, sessionId, threadId ?? "main");
  return await readBrowserView<SessionViewState>(key) ?? null;
}

export async function rememberSessionSelection(port: number, selection: SessionSelection | null): Promise<void> {
  await updateBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"), () => selection ?? undefined);
}

export async function readLatestSessionView(port: number): Promise<SessionViewState | null> {
  const selection = await readBrowserView<SessionSelection>(browserViewKey(await browserGatewayId(port), "selection"));
  if (!selection) return null;
  return await readSessionView(port, selection.workspace_id, selection.session_id) ?? {
    ...selection, turn_anchor: null, scroll_offset: 0, follow_latest: true,
    projection_version: 1, tool_details_expanded: false, updated_at: "",
  };
}

export async function writeSessionView(port: number, workspaceId: string, sessionId: string, payload: Omit<SessionViewState, "workspace_id" | "session_id" | "updated_at">, threadId?: string): Promise<SessionViewState> {
  const gatewayId = await browserGatewayId(port);
  const value: SessionViewState = { ...payload, workspace_id: workspaceId, session_id: sessionId, updated_at: new Date().toISOString() };
  await updateBrowserView(browserViewKey(gatewayId, "session", workspaceId, sessionId, threadId ?? "main"), () => value);
  return value;
}
