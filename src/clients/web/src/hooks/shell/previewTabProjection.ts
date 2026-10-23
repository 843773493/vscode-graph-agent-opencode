import type {
  WorkspacePreviewTab,
} from "../../components/workspace/WorkspaceFilePreviewArea";
import type { WorkspaceRuntimePreviewTab } from "../../components/workspace/WorkspaceRuntimePreviewArea";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";

export interface WorkspacePreviewTabProjection {
  filePreviewTabs: WorkspacePreviewTab[];
  changePreviewTabs: WorkspacePreviewTab[];
  runtimePreviewTabs: WorkspaceRuntimePreviewTab[];
  /** 当前辅助标签真正消费的代码预览页签：更改标签看差异，其余看文件。 */
  codePreviewTabs: WorkspacePreviewTab[];
  /** 在与 `activePath` 匹配时保留它，否则退化到首个页签。 */
  activeCodePreviewPath: string | null;
  /** 文件预览区的当前路径：只认文件页签，不认差异页签。 */
  activeFilePath: string | null;
  activeRuntimePreview: WorkspaceRuntimePreviewTab | null;
  codePreviewLoadingPath: string | null;
  codePreviewError: string | null;
}

/**
 * 把工作区预览页签投影成各区域需要的切片。纯函数：不持有状态，也不发起请求，
 * 便于把「哪个页签属于哪个区域、当前活动路径是哪一条」这类判定钉在唯一实现里。
 */
export function projectWorkspacePreviewTabs({
  tabs,
  auxiliaryTab,
  activePath,
  loadingPath,
  error,
}: {
  tabs: WorkspacePreviewTab[];
  auxiliaryTab: WorkspaceAuxiliaryTab;
  activePath: string | null;
  loadingPath: string | null;
  error: string | null;
}): WorkspacePreviewTabProjection {
  const filePreviewTabs: WorkspacePreviewTab[] = [];
  const changePreviewTabs: WorkspacePreviewTab[] = [];
  const runtimePreviewTabs: WorkspaceRuntimePreviewTab[] = [];
  let hasActiveFileTab = false;
  let hasActiveChangeTab = false;
  let hasLoadingFileTab = false;
  let hasLoadingChangeTab = false;
  let activeRuntimePreview: WorkspaceRuntimePreviewTab | null = null;

  for (const tab of tabs) {
    switch (tab.previewType) {
      case "file":
      case "file-placeholder":
        filePreviewTabs.push(tab);
        if (tab.path === activePath) hasActiveFileTab = true;
        if (tab.path === loadingPath) hasLoadingFileTab = true;
        break;
      case "session-diff":
        changePreviewTabs.push(tab);
        if (tab.path === activePath) hasActiveChangeTab = true;
        if (tab.path === loadingPath) hasLoadingChangeTab = true;
        break;
      case "terminal":
      case "browser":
        runtimePreviewTabs.push(tab);
        if (activeRuntimePreview === null && tab.path === activePath) {
          activeRuntimePreview = tab;
        }
        break;
    }
  }

  const codePreviewTabs = auxiliaryTab === "changes"
    ? changePreviewTabs
    : filePreviewTabs;
  const hasActiveCodeTab = auxiliaryTab === "changes"
    ? hasActiveChangeTab
    : hasActiveFileTab;
  const activeCodePreviewPath = hasActiveCodeTab
    ? activePath
    : codePreviewTabs[0]?.path ?? null;
  const activeFilePath = hasActiveFileTab
    ? activePath
    : filePreviewTabs[0]?.path ?? null;
  const hasLoadingCodeTab = auxiliaryTab === "changes"
    ? hasLoadingChangeTab
    : hasLoadingFileTab;
  const codePreviewLoadingPath = hasLoadingCodeTab
    ? loadingPath
    : null;
  // 差异页签与文件页签各自只认自己的错误通道，避免把终端的错误显示到文件预览上。
  const codePreviewError = error && (
    (auxiliaryTab === "changes" && activePath?.startsWith("session-diff://")) ||
    (auxiliaryTab === "files" && activePath !== null &&
      !activePath.startsWith("terminal://") &&
      !activePath.startsWith("browser://") &&
      !activePath.startsWith("session-diff://"))
  )
    ? error
    : null;

  return {
    filePreviewTabs,
    changePreviewTabs,
    runtimePreviewTabs,
    codePreviewTabs,
    activeCodePreviewPath,
    activeFilePath,
    activeRuntimePreview,
    codePreviewLoadingPath,
    codePreviewError,
  };
}
