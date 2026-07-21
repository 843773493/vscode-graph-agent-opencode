import type {
  SessionChangeset,
  SessionChangesetListItem,
  SessionFileChange,
  WorkspaceFileNode,
} from "../../types/backend";
import SessionChangesTree from "./SessionChangesTree";
import WorkspaceFileTree from "./WorkspaceFileTree";

export type WorkspaceAuxiliaryTab = "changes" | "files";

interface WorkspaceAuxiliaryPanelProps {
  visible: boolean;
  flexRatio: number;
  tab: WorkspaceAuxiliaryTab;
  apiPort: number;
  workspaceId: string | null;
  workspaceName: string;
  workspaceRoot: string;
  activeFilePath: string | null;
  sessionChangesets: SessionChangesetListItem[];
  selectedChangesetId: string | null;
  activeChangeset: SessionChangeset | null;
  sessionChangesLoading: boolean;
  sessionChangesError: string | null;
  sessionChangesLoadedAt: string | null;
  searchOpen: boolean;
  collapseVersion: number;
  onTabChange: (tab: WorkspaceAuxiliaryTab) => void;
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
  activeFilePath,
  sessionChangesets,
  selectedChangesetId,
  activeChangeset,
  sessionChangesLoading,
  sessionChangesError,
  sessionChangesLoadedAt,
  searchOpen,
  collapseVersion,
  onTabChange,
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
    >
      <header className="auxiliary-titlebar">
        <nav className="auxiliary-tabs" aria-label="会话详情">
          <button
            type="button"
            className={tab === "changes" ? "active" : ""}
            onClick={() => onTabChange("changes")}
          >
            更改
          </button>
          <button
            type="button"
            className={tab === "files" ? "active" : ""}
            onClick={() => onTabChange("files")}
          >
            文件
          </button>
        </nav>
        <div className="auxiliary-title-actions" aria-label="文件视图操作">
          <button
            type="button"
            className={`auxiliary-icon-button${searchOpen ? " active" : ""}`}
            title="搜索"
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
      </header>
      <div
        className={`auxiliary-view-body auxiliary-changes-body${
          tab === "changes" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={tab !== "changes"}
      >
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
              <span>工作区未提交更改暂未接入；上方显示会话文件变更。</span>
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
      >
          <WorkspaceFileTree
            apiPort={apiPort}
            workspaceId={workspaceId}
            workspaceName={workspaceName}
            workspaceRoot={workspaceRoot}
            activeFilePath={activeFilePath}
            searchOpen={searchOpen}
            collapseVersion={collapseVersion}
            onOpenFile={onOpenFile}
            onStatusChange={onStatusChange}
          />
      </div>
    </aside>
  );
}
