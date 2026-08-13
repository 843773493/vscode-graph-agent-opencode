import type { ReactNode } from "react";
import type {
  SessionChangeset,
  SessionChangesetListItem,
  SessionFileChange,
  WorkspaceFileNode,
} from "../../types/backend";
import SessionChangesTree from "./SessionChangesTree";
import WorkspaceFileTree from "./WorkspaceFileTree";

export type WorkspaceAuxiliaryTab = "changes" | "files" | "resources" | "debug";

interface WorkspaceAuxiliaryPanelProps {
  visible: boolean;
  flexRatio: number;
  tab: WorkspaceAuxiliaryTab;
  apiPort: number;
  workspaceId: string | null;
  workspaceName: string;
  workspaceRoot: string;
  sessionId: string;
  sessionTitle: string;
  extensionWindow?: boolean;
  activeFilePath: string | null;
  sessionChangesets: SessionChangesetListItem[];
  selectedChangesetId: string | null;
  activeChangeset: SessionChangeset | null;
  sessionChangesLoading: boolean;
  sessionChangesError: string | null;
  sessionChangesLoadedAt: string | null;
  searchOpen: boolean;
  collapseVersion: number;
  expandedFileTreePaths: string[];
  onExpandedFileTreePathsChange: (paths: string[]) => void;
  resourcePanel: ReactNode;
  runtimePreview: ReactNode;
  debugPanel: ReactNode;
  onToggleSearch: () => void;
  onCollapseAll: () => void;
  onSelectSessionChangeset: (changesetId: string) => void;
  onRefreshSessionChanges: () => void;
  onOpenSessionChangeFile: (file: SessionFileChange) => void;
  onReviewSessionChangeFile: (
    file: SessionFileChange,
    reviewed: boolean,
  ) => Promise<void>;
  onOpenFile: (node: WorkspaceFileNode) => void;
  onStatusChange: (message: string) => void;
}

export default function WorkspaceAuxiliaryPanel({
  visible,
  flexRatio,
  tab,
  apiPort,
  workspaceId,
  workspaceName,
  workspaceRoot,
  sessionId,
  sessionTitle,
  extensionWindow = false,
  activeFilePath,
  sessionChangesets,
  selectedChangesetId,
  activeChangeset,
  sessionChangesLoading,
  sessionChangesError,
  sessionChangesLoadedAt,
  searchOpen,
  collapseVersion,
  expandedFileTreePaths,
  onExpandedFileTreePathsChange,
  resourcePanel,
  runtimePreview,
  debugPanel,
  onToggleSearch,
  onCollapseAll,
  onSelectSessionChangeset,
  onRefreshSessionChanges,
  onOpenSessionChangeFile,
  onReviewSessionChangeFile,
  onOpenFile,
  onStatusChange,
}: WorkspaceAuxiliaryPanelProps) {
  return (
    <aside
      className={`auxiliary-panel${visible ? "" : " preserve-mounted-hidden"}`}
      hidden={!visible}
      style={{ flexBasis: 0, flexGrow: flexRatio }}
      data-bt-surface="workspace"
    >
      <div
        className={`auxiliary-view-body auxiliary-changes-body${
          tab === "changes" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={tab !== "changes"}
        data-bt-surface="layout"
      >
          <div className="auxiliary-scope-context" aria-label="更改范围">
            <span>变更范围</span>
            <strong title={workspaceRoot || workspaceName}>{workspaceName || "当前工作区"}</strong>
            <span title={sessionTitle}>{sessionTitle || "当前会话"} · 仅会话文件变更</span>
          </div>
          <SessionChangesTree
            changesets={sessionChangesets}
            selectedChangesetId={selectedChangesetId}
            activeChangeset={activeChangeset}
            loading={sessionChangesLoading}
            error={sessionChangesError}
            loadedAt={sessionChangesLoadedAt}
            onSelectChangeset={onSelectSessionChangeset}
            onRefresh={onRefreshSessionChanges}
            onOpenFile={onOpenSessionChangeFile}
            onReviewFile={onReviewSessionChangeFile}
          />
          <section className="auxiliary-tree-section">
            <header>工作区更改</header>
            <div className="auxiliary-empty-row">
              <span className="codicon codicon-git-compare" aria-hidden="true" />
              <span>工作区未提交更改尚未接入；当前列表只显示本会话产生的文件变更。</span>
            </div>
          </section>
          <section className="auxiliary-tree-section">
            <header>其他文件</header>
            <div className="auxiliary-empty-row muted">
              <span className="codicon-lite">◇</span>
              <span>暂无可展示文件</span>
            </div>
          </section>
      </div>
      <div
        className={`auxiliary-view-body auxiliary-files-body${
          tab === "files" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={tab !== "files"}
        data-bt-surface="layout"
      >
          <div className="auxiliary-files-toolbar" aria-label="文件操作">
            <button
              type="button"
              className={`auxiliary-icon-button${searchOpen ? " active" : ""}`}
              title="搜索文件"
              aria-label="搜索文件"
              onClick={onToggleSearch}
            >
              <span className="auxiliary-action-icon search" aria-hidden="true" />
            </button>
            <button
              type="button"
              className="auxiliary-icon-button"
              title="全部折叠"
              aria-label="全部折叠"
              onClick={onCollapseAll}
            >
              <span className="auxiliary-action-icon collapse-all" aria-hidden="true" />
            </button>
          </div>
          <WorkspaceFileTree
            active={visible && tab === "files"}
            apiPort={apiPort}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            workspaceRoot={workspaceRoot}
            sessionId={sessionId}
            activeFilePath={activeFilePath}
            searchOpen={searchOpen}
            collapseVersion={collapseVersion}
            expandedPaths={expandedFileTreePaths}
            onExpandedPathsChange={onExpandedFileTreePathsChange}
            onCloseSearch={onToggleSearch}
            onOpenFile={onOpenFile}
            onStatusChange={onStatusChange}
          />
      </div>
      <div
        className={`auxiliary-view-body auxiliary-resources-body${extensionWindow ? " extension-window-body" : ""}${runtimePreview ? " has-runtime-preview" : ""}${
          tab === "resources" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={tab !== "resources"}
        data-extension-region={extensionWindow ? "workspace" : undefined}
        data-bt-surface="layout"
      >
        {runtimePreview}
        {resourcePanel}
      </div>
      <div
        className={`auxiliary-view-body auxiliary-debug-body${tab === "debug" ? "" : " preserve-mounted-hidden"}`}
        hidden={tab !== "debug"}
        data-bt-surface="layout"
      >
        {debugPanel}
      </div>
    </aside>
  );
}
