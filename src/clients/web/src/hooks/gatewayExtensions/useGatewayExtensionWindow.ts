import type { Dispatch, SetStateAction } from "react";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";
import type { WorkspaceRuntimePreviewTab } from "../../components/workspace/WorkspaceRuntimePreviewArea";
import { createSessionConnection } from "../../api/gateway/sessionConnections";
import type {
  GatewayExtensionResourceEntry,
  useGatewayExtensionResources,
} from "./useGatewayExtensionResources";
import type { SessionResource } from "../../types/backend";
import { buildGatewayAttachUrl } from "../../utils/attachUrls";
import {
  EXTENSION_WINDOW_NAME,
  buildExtensionWindowUrl,
  type ExtensionResourceKind,
} from "../../utils/extensionResourceWindow";

interface UseGatewayExtensionWindowOptions {
  extensionWindowRequested: boolean;
  extensionWindowFallback: boolean;
  setExtensionWindowFallback: Dispatch<SetStateAction<boolean>>;
  auxiliaryTab: WorkspaceAuxiliaryTab;
  sharedPreviewVisible: boolean;
  activeRuntimePreview: WorkspaceRuntimePreviewTab | null;
  sessionResources: SessionResource[];
  extensionResources: ReturnType<typeof useGatewayExtensionResources>;
  openAuxiliaryTab: (tab: WorkspaceAuxiliaryTab) => void;
  openBrowserPreview: (browserId: string) => void;
  openTerminalPreview: (terminalId: string) => void;
  apiPort: number;
  activeSessionWorkspaceId: string | null;
  activeSessionId: string | null;
  setStatus: (text: string) => void;
}

/** 扩展窗口模式与 Gateway 扩展资源的编排：可见性、预览标签选择与打开/退出动作。 */
export function useGatewayExtensionWindow({
  extensionWindowRequested,
  extensionWindowFallback,
  setExtensionWindowFallback,
  auxiliaryTab,
  sharedPreviewVisible,
  activeRuntimePreview,
  sessionResources,
  extensionResources,
  openAuxiliaryTab,
  openBrowserPreview,
  openTerminalPreview,
  apiPort,
  activeSessionWorkspaceId,
  activeSessionId,
  setStatus,
}: UseGatewayExtensionWindowOptions) {
  const openExtensionWindow = (kind: ExtensionResourceKind, resourceId?: string) => {
    if (extensionWindowRequested) {
      if (kind === "debug") {
        openAuxiliaryTab("debug");
        return;
      }
      const entry = extensionResources.entries.find(
        (candidate) =>
          candidate.resource.kind === kind &&
          candidate.resource.resource_id === resourceId,
      );
      if (entry) {
        extensionResources.select(entry.key);
      }
      return;
    }

    const url = buildExtensionWindowUrl({
      kind,
      resourceId,
      workspaceId: activeSessionWorkspaceId,
      sessionId: activeSessionId,
    });
    const extensionWindow = window.open(url, EXTENSION_WINDOW_NAME);
    if (!extensionWindow) {
      setExtensionWindowFallback(true);
      openAuxiliaryTab(kind === "debug" ? "debug" : "resources");
      if (kind === "browser") {
        openBrowserPreview(resourceId ?? "");
      } else if (kind === "terminal") {
        openTerminalPreview(resourceId ?? "");
      }
      setStatus("扩展窗口未能打开，已在当前页面切换为扩展窗口模式；请检查浏览器弹窗权限。");
      return;
    }
    extensionWindow.focus();
    setStatus("已打开扩展窗口；后续扩展内容将在此窗口内切换。");
  };
  const openExtensionResource = (entry: GatewayExtensionResourceEntry) => {
    extensionResources.select(entry.key);
    setStatus(
      `已切换到 ${entry.gateway_name} · ${entry.workspace_name} · ${entry.session_title}`,
    );
  };
  const createExtensionReplacement = async (entry: GatewayExtensionResourceEntry) => {
    const created = await createSessionConnection(
      apiPort,
      entry.workspace_id,
      entry.session_id,
      "browser",
    );
    await extensionResources.refresh();
    setStatus(`已新建浏览器：${created.resourceId}`);
  };
  const selectedExtensionEntry = extensionResources.selectedEntry;
  const extensionPreviewEntry = selectedExtensionEntry &&
    (selectedExtensionEntry.resource.kind === "browser" ||
      selectedExtensionEntry.resource.kind === "terminal")
    ? selectedExtensionEntry
    : null;
  const extensionPreviewTab: WorkspaceRuntimePreviewTab | null = extensionPreviewEntry
    ? extensionPreviewEntry.resource.kind === "browser"
      ? {
          previewType: "browser",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewEntry.resource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          browserId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "browser",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
          ),
        }
      : {
          previewType: "terminal",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewEntry.resource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          terminalId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "terminal",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
          ),
        }
    : null;
  const handleExitExtensionWindow = () => {
    if (extensionWindowRequested) {
      if (window.opener && !window.opener.closed) {
        window.close();
        return;
      }
      const standardUrl = new URL(window.location.href);
      standardUrl.pathname = "/";
      standardUrl.search = "";
      standardUrl.hash = "";
      window.location.assign(standardUrl.toString());
      return;
    }
    setExtensionWindowFallback(false);
  };
  const extensionWindowVisible = extensionWindowRequested || extensionWindowFallback;
  const extensionDebugSplitActive = extensionWindowVisible &&
    auxiliaryTab === "debug" &&
    sharedPreviewVisible;
  const activeRuntimePreviewResource = activeRuntimePreview
    ? sessionResources.find((resource) =>
        resource.resource_id === (
          activeRuntimePreview.previewType === "browser"
            ? activeRuntimePreview.browserId
            : activeRuntimePreview.terminalId
        )
        && resource.kind === activeRuntimePreview.previewType
        && resource.status === "running",
      ) ?? null
    : null;
  const runtimePreviewTab = extensionWindowRequested
    ? extensionPreviewTab
    : extensionWindowFallback && activeRuntimePreviewResource
      ? activeRuntimePreview
      : null;

  return {
    openExtensionWindow,
    openExtensionResource,
    createExtensionReplacement,
    handleExitExtensionWindow,
    extensionWindowVisible,
    extensionDebugSplitActive,
    runtimePreviewTab,
  };
}
