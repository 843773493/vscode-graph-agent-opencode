import type { Dispatch, SetStateAction } from "react";
import type { WorkspaceAuxiliaryTab } from "../components/workspace/WorkspaceAuxiliaryPanel";
import type { WorkspaceBottomPanelState } from "../state/workspaceBottomPanel";
import type { WebUiSettingsUpdate } from "../types/backend";

interface UseWorkbenchPanelRoutingOptions {
  auxiliaryVisible: boolean;
  setAuxiliaryVisible: Dispatch<SetStateAction<boolean>>;
  chatVisible: boolean;
  setChatVisible: Dispatch<SetStateAction<boolean>>;
  setAuxiliaryTab: Dispatch<SetStateAction<WorkspaceAuxiliaryTab>>;
  setAuxiliaryTabOrder: Dispatch<SetStateAction<WorkspaceAuxiliaryTab[]>>;
  bottomPanelState: WorkspaceBottomPanelState;
  updateBottomPanelState: (patch: Partial<WorkspaceBottomPanelState>) => void;
  bottomPanelWorkspaceId: string | null;
  extensionWindowRequested: boolean;
  setExtensionWindowFallback: Dispatch<SetStateAction<boolean>>;
  persistLayoutSettings: (layout: WebUiSettingsUpdate["layout"]) => void;
  setStatus: (text: string) => void;
}

/** 工作台辅助面板路由：右侧侧边栏、会话区与底部面板的显隐、标签与终端切换。 */
export function useWorkbenchPanelRouting({
  auxiliaryVisible,
  setAuxiliaryVisible,
  chatVisible,
  setChatVisible,
  setAuxiliaryTab,
  setAuxiliaryTabOrder,
  bottomPanelState,
  updateBottomPanelState,
  bottomPanelWorkspaceId,
  extensionWindowRequested,
  setExtensionWindowFallback,
  persistLayoutSettings,
  setStatus,
}: UseWorkbenchPanelRoutingOptions) {
  const handleToggleAuxiliaryPanel = () => {
    const nextVisible = !auxiliaryVisible;
    setAuxiliaryVisible(nextVisible);
    persistLayoutSettings({ auxiliary_visible: nextVisible });
    setStatus(nextVisible ? "右侧侧边栏已切换为展开" : "右侧侧边栏已切换为收起");
  };
  const handleToggleChatPanel = () => {
    const nextVisible = !chatVisible;
    setChatVisible(nextVisible);
    persistLayoutSettings({ chat_visible: nextVisible });
    setStatus(nextVisible ? "会话区已展开" : "会话区已收起");
  };
  const handleTogglePanel = () => {
    const nextVisible = !bottomPanelState.visible;
    updateBottomPanelState({ visible: nextVisible });
    setStatus(nextVisible ? "底部面板已展开" : "底部面板已收起");
  };
  const handleAuxiliaryTabChange = (tab: WorkspaceAuxiliaryTab) => {
    setAuxiliaryTab(tab);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_tab: tab });
    }
  };

  const handleAuxiliaryTabReorder = (tabOrder: WorkspaceAuxiliaryTab[]) => {
    setAuxiliaryTabOrder(tabOrder);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_tab_order: tabOrder });
    }
  };
  const openAuxiliaryTab = (tab: WorkspaceAuxiliaryTab) => {
    if (tab !== "resources") {
      setExtensionWindowFallback(false);
    }
    setAuxiliaryVisible(true);
    setAuxiliaryTab(tab);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: tab });
    }
  };
  const openTerminalPanel = (terminalId: string) => {
    if (!bottomPanelWorkspaceId) {
      setStatus("打开终端失败：当前没有活动工作区");
      return;
    }
    updateBottomPanelState({
      visible: true,
      tab: "terminal",
      terminalId,
    });
    setStatus(`已在主窗口底部面板打开终端：${terminalId}`);
  };

  return {
    handleToggleAuxiliaryPanel,
    handleToggleChatPanel,
    handleTogglePanel,
    handleAuxiliaryTabChange,
    handleAuxiliaryTabReorder,
    openAuxiliaryTab,
    openTerminalPanel,
  };
}
