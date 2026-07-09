import type { CSSProperties } from "react";
import type { WorkspaceFileContent } from "../../types/backend";

interface WorkspaceFilePreviewAreaProps {
  width: number;
  tabs: WorkspaceFileContent[];
  activePath: string | null;
  loadingPath: string | null;
  error: string | null;
  onSelectTab: (path: string) => void;
  onCloseTab: (path: string) => void;
  onClosePanel: () => void;
}

function formatFileSize(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function lineRows(content: string): string[] {
  const normalized = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return normalized.split("\n");
}

export default function WorkspaceFilePreviewArea({
  width,
  tabs,
  activePath,
  loadingPath,
  error,
  onSelectTab,
  onCloseTab,
  onClosePanel,
}: WorkspaceFilePreviewAreaProps) {
  const activeTab = tabs.find((tab) => tab.path === activePath) ?? tabs[0] ?? null;
  const activeLines = activeTab ? lineRows(activeTab.content) : [];
  const lineNumberWidth = Math.max(2, String(activeLines.length).length);
  const showLoadingEmpty = loadingPath && !activeTab;

  return (
    <section
      className="workspace-preview-panel"
      style={{ flexBasis: width, width }}
      aria-label="文件预览区"
    >
      <header className="workspace-preview-tabs">
        <div className="workspace-preview-tab-strip">
          {tabs.length > 0 ? (
            tabs.map((tab) => (
              <div
                key={tab.path}
                className={`workspace-preview-tab${activeTab?.path === tab.path ? " active" : ""}`}
              >
                <button
                  type="button"
                  className="workspace-preview-tab-main"
                  title={tab.path}
                  onClick={() => onSelectTab(tab.path)}
                >
                  <span className="workspace-preview-tab-icon">◇</span>
                  <span className="workspace-preview-tab-label">{tab.name}</span>
                </button>
                <button
                  type="button"
                  className="workspace-preview-tab-close"
                  title={`关闭 ${tab.name}`}
                  aria-label={`关闭 ${tab.name}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onCloseTab(tab.path);
                  }}
                >
                  ×
                </button>
              </div>
            ))
          ) : (
            <div className="workspace-preview-tab-placeholder">预览</div>
          )}
        </div>
        <div className="workspace-preview-actions">
          <button
            type="button"
            className="workspace-preview-panel-close"
            title="隐藏文件预览"
            aria-label="隐藏文件预览"
            onClick={onClosePanel}
          >
            ×
          </button>
        </div>
      </header>

      <div className="workspace-preview-content">
        {activeTab ? (
          <>
            <div className="workspace-preview-toolbar">
              <span className="workspace-preview-title">{activeTab.path}</span>
              <span className="workspace-preview-meta">
                {activeTab.language} · {formatFileSize(activeTab.size)}
              </span>
            </div>
            <div
              className="workspace-preview-code-scroll"
              data-loading={String(loadingPath === activeTab.path)}
            >
              <div
                className="workspace-preview-code-table"
                style={{ "--preview-line-number-width": `${lineNumberWidth}ch` } as CSSProperties}
              >
                {activeLines.map((line, index) => (
                  <div className="workspace-preview-line" key={`${index}-${line.length}`}>
                    <span className="workspace-preview-line-number">{index + 1}</span>
                    <code className="workspace-preview-line-code">
                      {line.length > 0 ? line : "\u00A0"}
                    </code>
                  </div>
                ))}
              </div>
            </div>
          </>
        ) : (
          <div className="workspace-preview-empty">
            <span className="workspace-preview-empty-icon">▱</span>
            <span>{showLoadingEmpty ? "正在读取文件..." : "从右侧文件树选择文件预览"}</span>
          </div>
        )}

        {loadingPath ? (
          <div className="workspace-preview-status">正在读取 {loadingPath}</div>
        ) : null}
        {error ? (
          <div className="workspace-preview-error" role="alert">
            {error}
          </div>
        ) : null}
      </div>
    </section>
  );
}
