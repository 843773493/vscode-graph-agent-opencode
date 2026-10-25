import type {
  WebUiBottomPanelTab,
  WebUiWorkspaceBottomPanelSettings,
} from "../types/backend";

export interface WorkspaceBottomPanelState {
  visible: boolean;
  height: number;
  tab: WebUiBottomPanelTab;
  terminalId: string | null;
}

/** 用户打开的本次界面错误快照，不依赖服务端日志请求。 */
export interface OutputDiagnostic {
  title: string;
  detail: string;
}

export function resolveWorkspaceBottomPanelState(
  persisted: WebUiWorkspaceBottomPanelSettings | null | undefined,
  fallback: WorkspaceBottomPanelState,
): WorkspaceBottomPanelState {
  return {
    visible: persisted?.visible ?? fallback.visible,
    height: persisted?.height ?? fallback.height,
    tab: persisted?.tab ?? fallback.tab,
    terminalId: persisted?.terminal_id ?? fallback.terminalId,
  };
}

export function toWorkspaceBottomPanelSettings(
  state: WorkspaceBottomPanelState,
): WebUiWorkspaceBottomPanelSettings {
  return {
    visible: state.visible,
    height: state.height,
    tab: state.tab,
    terminal_id: state.terminalId,
  };
}
