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

export interface WorkspaceBottomPanelFallback {
  visible: boolean;
  height: number;
  tab: WebUiBottomPanelTab;
  terminalId: string | null;
}

export function resolveWorkspaceBottomPanelState(
  persisted: WebUiWorkspaceBottomPanelSettings | null | undefined,
  fallback: WorkspaceBottomPanelFallback,
): WorkspaceBottomPanelState {
  return {
    visible: persisted?.visible ?? fallback.visible,
    height: persisted?.height ?? fallback.height,
    tab: persisted?.tab === "gateway" ? "output" : persisted?.tab ?? fallback.tab,
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
