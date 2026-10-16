import type { ResourceThreadRef, WorkspaceResourceDTO } from "../../api/workspaceResources";
import type { SessionResourceRefreshResult } from "../session/useSessionResourceLoader";

export function closeChildConversationAndClearForkSelection(
  closeConversation: () => void,
  clearForkSelection: () => void,
): void {
  closeConversation();
  clearForkSelection();
}

export async function createTerminalFromResourcePanel({
  sessionId,
  thread,
  createTerminal,
  refreshSessionResources,
  closeChildConversation,
  openTerminalPanel,
}: {
  sessionId: string;
  thread: ResourceThreadRef;
  createTerminal: (thread: ResourceThreadRef, retentionScope: "thread") => Promise<WorkspaceResourceDTO>;
  refreshSessionResources: (
    sessionId: string,
    options: { force: true },
  ) => Promise<SessionResourceRefreshResult>;
  closeChildConversation: () => void;
  openTerminalPanel: (terminalId: string) => void;
}): Promise<WorkspaceResourceDTO> {
  const created = await createTerminal(thread, "thread");
  const refresh = await refreshSessionResources(sessionId, { force: true });
  if (refresh !== "loaded") {
    const reason = refresh === "failed" ? "会话连接列表刷新失败" : "当前会话已切换";
    throw new Error(`终端 ${created.resource_id} 已创建，但${reason}；子线程仍保持打开，请刷新资源后再打开终端。`);
  }
  closeChildConversation();
  openTerminalPanel(created.resource_id);
  return created;
}
