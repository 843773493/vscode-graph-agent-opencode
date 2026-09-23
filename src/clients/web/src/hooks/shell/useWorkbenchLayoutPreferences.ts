import { useCallback, useEffect, useMemo, useState } from "react";
import { errorMessage } from "../../utils/errorMessage";
import type { WorkbenchView } from "../../components/shell/Toolbar";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";
import {
  DEFAULT_GATEWAY_PANEL_HEIGHT,
  clampGatewayPanelHeight,
  defaultAuxiliaryVisible,
  resolveMainAreaRatios,
} from "../../layout/workbenchLayout";
import {
  resolveWorkspaceBottomPanelState,
  toWorkspaceBottomPanelSettings,
  type WorkspaceBottomPanelState,
} from "../../state/workspaceBottomPanel";
import type { ExtensionWindowRequest } from "../../utils/extensionResourceWindow";

const DEFAULT_AUXILIARY_TAB_ORDER: WorkspaceAuxiliaryTab[] = [
  "files",
  "changes",
  "debug",
  "resources",
];

interface UseWorkbenchLayoutPreferencesInput {
  uiSettings: WebUiSettings;
  /** 扩展窗口请求；非空表示当前运行在扩展窗口里，布局偏好不由主窗口设置驱动。 */
  extensionWindowRequest: ExtensionWindowRequest | null;
  /** 底部面板状态归属的 Gateway 工作区；为空时不落盘。 */
  bottomPanelWorkspaceId: string | null;
  updateUiSettings: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
  setStatus: (text: string) => void;
}

/**
 * AppShell 的工作台布局偏好链路：把后端权威的 UI 设置映射成外壳本地的展示态，
 * 并负责把用户操作落库。底部面板状态按工作区保存在本地镜像里，落盘由后端设置承担。
 */
export function useWorkbenchLayoutPreferences({
  uiSettings,
  extensionWindowRequest,
  bottomPanelWorkspaceId,
  updateUiSettings,
  setStatus,
}: UseWorkbenchLayoutPreferencesInput) {
  const extensionWindowRequested = extensionWindowRequest !== null;
  const [workbenchView, setWorkbenchView] = useState<WorkbenchView>(
    () => uiSettings.layout.workbench_view ?? "sessions",
  );
  const [auxiliaryTab, setAuxiliaryTab] = useState<WorkspaceAuxiliaryTab>(
    () => extensionWindowRequested
      ? extensionWindowRequest?.kind === "debug" ? "debug" : "resources"
      : uiSettings.layout.auxiliary_tab ?? "files",
  );
  const [auxiliaryTabOrder, setAuxiliaryTabOrder] = useState<WorkspaceAuxiliaryTab[]>(
    () => uiSettings.layout.auxiliary_tab_order
      ? [...uiSettings.layout.auxiliary_tab_order]
      : [...DEFAULT_AUXILIARY_TAB_ORDER],
  );
  const [auxiliaryVisible, setAuxiliaryVisible] = useState(
    () => extensionWindowRequested
      ? true
      : uiSettings.layout.auxiliary_visible ?? defaultAuxiliaryVisible(),
  );
  const [chatVisible, setChatVisible] = useState(
    () => extensionWindowRequested
      ? false
      : uiSettings.layout.chat_visible ?? true,
  );
  const [extensionWindowFallback, setExtensionWindowFallback] = useState(false);
  const [workspaceBottomPanelStates, setWorkspaceBottomPanelStates] = useState<
    Record<string, WorkspaceBottomPanelState>
  >({});
  const [mainAreaRatios, setMainAreaRatios] = useState(() =>
    resolveMainAreaRatios(uiSettings.layout.main_area_ratios),
  );

  const bottomPanelState = useMemo(() => {
    const persisted = bottomPanelWorkspaceId
      ? uiSettings.layout.bottom_panel_by_workspace?.[bottomPanelWorkspaceId]
      : null;
    return workspaceBottomPanelStates[bottomPanelWorkspaceId ?? ""] ??
      resolveWorkspaceBottomPanelState(persisted, {
        visible: extensionWindowRequested
          ? false
          : uiSettings.layout.panel_visible ?? false,
        height: clampGatewayPanelHeight(
          uiSettings.layout.panel_height ?? DEFAULT_GATEWAY_PANEL_HEIGHT,
        ),
        tab: "output",
        terminalId: null,
      });
  }, [
    bottomPanelWorkspaceId,
    extensionWindowRequested,
    uiSettings.layout.bottom_panel_by_workspace,
    uiSettings.layout.panel_height,
    uiSettings.layout.panel_visible,
    workspaceBottomPanelStates,
  ]);
  const panelVisible = !extensionWindowRequested && bottomPanelState.visible;

  useEffect(() => {
    const layout = uiSettings.layout;
    if (extensionWindowRequested) {
      return;
    }
    if (layout.workbench_view) {
      setWorkbenchView(layout.workbench_view);
    }
    if (typeof layout.auxiliary_visible === "boolean") {
      setAuxiliaryVisible(layout.auxiliary_visible);
    }
    if (typeof layout.chat_visible === "boolean") {
      setChatVisible(layout.chat_visible);
    }
    if (layout.auxiliary_tab) {
      setAuxiliaryTab(layout.auxiliary_tab);
    }
    if (layout.auxiliary_tab_order) {
      setAuxiliaryTabOrder([...layout.auxiliary_tab_order]);
    }
    setMainAreaRatios(resolveMainAreaRatios(layout.main_area_ratios));
  }, [extensionWindowRequest?.kind, extensionWindowRequested, uiSettings]);

  const persistUiSettings = useCallback(
    (
      input: WebUiSettingsUpdate
        | ((current: WebUiSettings) => WebUiSettingsUpdate),
    ) => {
      void updateUiSettings(input).catch((error: unknown) => {
        setStatus(`保存页面设置失败: ${errorMessage(error)}`);
      });
    },
    [setStatus, updateUiSettings],
  );
  const persistLayoutSettings = useCallback(
    (layout: WebUiSettingsUpdate["layout"]) => {
      persistUiSettings({ layout });
    },
    [persistUiSettings],
  );
  const handleWorkbenchViewChange = useCallback(
    (view: WorkbenchView) => {
      setWorkbenchView(view);
      persistLayoutSettings({ workbench_view: view });
    },
    [persistLayoutSettings],
  );

  const updateBottomPanelState = useCallback(
    (patch: Partial<WorkspaceBottomPanelState>) => {
      if (!bottomPanelWorkspaceId) {
        return;
      }
      const nextState: WorkspaceBottomPanelState = {
        ...bottomPanelState,
        ...patch,
      };
      setWorkspaceBottomPanelStates((previous) => ({
        ...previous,
        [bottomPanelWorkspaceId]: nextState,
      }));
      persistUiSettings((current) => ({
        layout: {
          bottom_panel_by_workspace: {
            ...(current.layout.bottom_panel_by_workspace ?? {}),
            [bottomPanelWorkspaceId]: toWorkspaceBottomPanelSettings(nextState),
          },
        },
      }));
    },
    [bottomPanelState, bottomPanelWorkspaceId, persistUiSettings],
  );

  return {
    workbenchView,
    setWorkbenchView,
    handleWorkbenchViewChange,
    auxiliaryTab,
    setAuxiliaryTab,
    auxiliaryTabOrder,
    setAuxiliaryTabOrder,
    auxiliaryVisible,
    setAuxiliaryVisible,
    chatVisible,
    setChatVisible,
    mainAreaRatios,
    setMainAreaRatios,
    bottomPanelState,
    panelVisible,
    setWorkspaceBottomPanelStates,
    extensionWindowFallback,
    setExtensionWindowFallback,
    persistUiSettings,
    persistLayoutSettings,
    updateBottomPanelState,
  };
}
