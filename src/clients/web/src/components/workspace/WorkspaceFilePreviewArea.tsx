import { failureSummary } from "../../../../shared/core/failurePresentation";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import type {
  NodeDebugBreakpoint,
  SessionFileChange,
  WorkspaceFileContent,
} from "../../types/backend";
import type { WorkspaceFileSelection } from "../../utils/workspaceFileReferences";
import { formatByteSize } from "../../utils/format";
import NodeDebugBreakpointGutter, {
  type NodeDebugBreakpointDefinition,
} from "../nodeDebug/NodeDebugBreakpointGutter";
import WorkspaceMarkdownPreview from "./preview/WorkspaceMarkdownPreview";
import WorkspaceCodePreview from "./preview/WorkspaceCodePreview";

export type WorkspacePreviewTab =
  | (WorkspaceFileContent & {
      previewType: "file";
      selection: WorkspaceFileSelection | null;
      fragment?: string;
    })
  | {
      previewType: "file-placeholder";
      path: string;
      name: string;
    }
  | {
      previewType: "terminal";
      path: string;
      name: string;
      scopeLabel?: string;
      terminalId: string;
      attachUrl: string;
    }
  | {
      previewType: "browser";
      path: string;
      name: string;
      scopeLabel?: string;
      browserId: string;
      attachUrl: string;
    }
  | {
      previewType: "session-diff";
      path: string;
      name: string;
      change: SessionFileChange;
      changesetLabel: string;
    };

interface WorkspaceFilePreviewAreaProps {
  context: "files" | "changes";
  visible: boolean;
  flexRatio: number;
  apiPort: number;
  workspaceId: string | null;
  workspaceName: string;
  sessionTitle: string;
  tabs: WorkspacePreviewTab[];
  activePath: string | null;
  loadingPath: string | null;
  error: string | null;
  editingPath: string | null;
  draftContent: string;
  savingPath: string | null;
  hasUnsavedEdit: boolean;
  markdownSourceVisible: boolean;
  onMarkdownSourceChange: (visible: boolean) => void;
  onBeginEdit: (path: string) => void;
  onDraftChange: (content: string) => void;
  onCancelEdit: () => void;
  onSaveEdit: () => Promise<void>;
  onOpenWorkspacePath: (path: string, fragment?: string) => Promise<void>;
  onClose?: () => void;
  debugMode?: boolean;
  debugExecutionPath?: string | null;
  debugExecutionLine?: number | null;
  debugBreakpoints?: NodeDebugBreakpoint[];
  debugActionBusy?: boolean;
  onChangeDebugBreakpoint?: (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => void;
}

// 会话 diff 可能包含数万行；一次渲染全部行会让主线程长时间阻塞。超过阈值时
// 只渲染前 N 行，并显式提示剩余行数与展开入口，绝不静默截断。
const MAX_RENDERED_DIFF_LINES = 2000;
const EMPTY_DEBUG_BREAKPOINTS: NodeDebugBreakpoint[] = [];

function lineRows(content: string): string[] {
  const normalized = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return normalized.split("\n");
}

function projectDiffLines(
  content: string,
  maxRenderedLines: number | null,
): { lines: string[]; lineCount: number } {
  const lines: string[] = [];
  let lineStart = 0;
  let lineCount = 1;

  for (let index = 0; index < content.length; index += 1) {
    const character = content.charCodeAt(index);
    if (character !== 10 && character !== 13) continue;

    if (maxRenderedLines === null || lines.length < maxRenderedLines) {
      lines.push(content.slice(lineStart, index));
    }
    if (character === 13 && content.charCodeAt(index + 1) === 10) index += 1;
    lineStart = index + 1;
    lineCount += 1;
  }

  if (maxRenderedLines === null || lines.length < maxRenderedLines) {
    lines.push(content.slice(lineStart));
  }

  return { lines, lineCount };
}

function diffLineKind(line: string): "add" | "remove" | "meta" | "context" {
  if (line.startsWith("+") && !line.startsWith("+++")) {
    return "add";
  }
  if (line.startsWith("-") && !line.startsWith("---")) {
    return "remove";
  }
  if (
    line.startsWith("@@") ||
    line.startsWith("---") ||
    line.startsWith("+++")
  ) {
    return "meta";
  }
  return "context";
}

export default function WorkspaceFilePreviewArea({
  context,
  visible,
  flexRatio,
  apiPort,
  workspaceId,
  workspaceName,
  sessionTitle,
  tabs,
  activePath,
  loadingPath,
  error,
  editingPath,
  draftContent,
  savingPath,
  hasUnsavedEdit,
  markdownSourceVisible,
  onMarkdownSourceChange,
  onBeginEdit,
  onDraftChange,
  onCancelEdit,
  onSaveEdit,
  onOpenWorkspacePath,
  onClose,
  debugMode = false,
  debugExecutionPath = null,
  debugExecutionLine = null,
  debugBreakpoints = EMPTY_DEBUG_BREAKPOINTS,
  debugActionBusy = false,
  onChangeDebugBreakpoint,
}: WorkspaceFilePreviewAreaProps) {
  const debugExecutionLineRef = useRef<HTMLDivElement | null>(null);
  const activeTab = useMemo(
    () => tabs.find((tab) => tab.path === activePath) ?? tabs[0] ?? null,
    [activePath, tabs],
  );
  // 展开状态按标签路径记忆：切换标签或重开同一 diff 时重置为折叠，避免上次
  // 的展开状态在另一个大 diff 上意外生效。
  const [diffFullyExpanded, setDiffFullyExpanded] = useState(false);
  useEffect(() => {
    setDiffFullyExpanded(false);
  }, [activeTab?.path]);
  const activeDiffText = activeTab?.previewType === "session-diff"
    ? activeTab.change.diff_text
    : null;
  const activeDiffProjection = useMemo(
    () => activeDiffText === null
      ? { lines: [], lineCount: 0 }
      : projectDiffLines(
        activeDiffText,
        diffFullyExpanded ? null : MAX_RENDERED_DIFF_LINES,
      ),
    [activeDiffText, diffFullyExpanded],
  );
  const diffTruncated = activeDiffProjection.lineCount > MAX_RENDERED_DIFF_LINES;
  const visibleDiffLines = activeDiffProjection.lines;
  const diffLineNumberWidth = Math.max(2, String(activeDiffProjection.lineCount).length);
  const showLoadingEmpty = loadingPath &&
    (!activeTab || activeTab.previewType === "file-placeholder");
  const editingActiveFile = activeTab?.previewType === "file" &&
    editingPath === activeTab.path;
  const activeEditorContent = activeTab?.previewType === "file"
    ? editingActiveFile ? draftContent : activeTab.content
    : "";
  const markdownActiveFile = activeTab?.previewType === "file" &&
    activeTab.language === "markdown";
  const debugActiveFile = debugMode && activeTab?.previewType === "file";
  const activeEditorLines = useMemo(
    () => debugActiveFile && !editingActiveFile ? lineRows(activeEditorContent) : [],
    [activeEditorContent, debugActiveFile, editingActiveFile],
  );
  const activeDebugLine = debugActiveFile && activeTab.path === debugExecutionPath
    ? debugExecutionLine
    : null;
  const debugBreakpointByLine = useMemo(
    () => new Map(
      debugBreakpoints
        .filter((breakpoint) => breakpoint.path === activeTab?.path)
        .map((breakpoint) => [breakpoint.actual_line ?? breakpoint.line, breakpoint]),
    ),
    [activeTab?.path, debugBreakpoints],
  );

  useEffect(() => {
    if (!activeDebugLine) return;
    debugExecutionLineRef.current?.scrollIntoView({ block: "center", inline: "nearest" });
  }, [activeDebugLine, activeTab?.path]);

  return (
    <section
      className={`workspace-preview-panel${visible ? "" : " preserve-mounted-hidden"}`}
      hidden={!visible}
      style={{ flexBasis: 0, flexGrow: flexRatio }}
      aria-label={context === "changes" ? t("workspace.preview.changesRegion") : t("workspace.preview.fileRegion")}
      data-bt-surface="workspace"
    >
      <div className="workspace-preview-content">
        {activeTab?.previewType === "file" ? (
          <div className="workspace-preview-toolbar workspace-file-preview-toolbar">
            <div className="workspace-preview-title-group">
              <span className="workspace-preview-title" title={activeTab.path}>
                {activeTab.name}
              </span>
              <span
                className="workspace-preview-scope"
                title={`${workspaceName || t("workspace.auxiliary.currentWorkspace")} · ${sessionTitle || t("workspace.auxiliary.currentSession")}`}
              >
                {context === "changes" ? t("workspace.preview.sessionChanges") : workspaceName || t("workspace.auxiliary.currentWorkspace")} · {sessionTitle || t("workspace.auxiliary.currentSession")}
              </span>
            </div>
            <div className="workspace-preview-toolbar-actions" aria-label={t("workspace.preview.fileActions")}>
              {onClose ? <button type="button" className="auxiliary-icon-button" title={t("workspace.preview.closeFilePreview")} aria-label={t("workspace.preview.closeFilePreview")} onClick={onClose}>
                <span className="codicon codicon-close" aria-hidden="true" />
              </button> : null}
              <span className="workspace-preview-meta">
                {activeTab.language} · {formatByteSize(activeTab.size)}
              </span>
              {activeTab.language === "markdown" ? (
                <div className="workspace-preview-mode-switch" role="group" aria-label={t("workspace.preview.markdown.viewMode")}>
                  <button
                    type="button"
                    className={markdownSourceVisible ? "active" : ""}
                    aria-pressed={markdownSourceVisible}
                    onClick={() => onMarkdownSourceChange(true)}
                  >
                    {t("workspace.preview.markdown.source")}
                  </button>
                  <button
                    type="button"
                    className={!markdownSourceVisible ? "active" : ""}
                    aria-pressed={!markdownSourceVisible}
                    onClick={() => onMarkdownSourceChange(false)}
                  >
                    {t("workspace.preview.markdown.preview")}
                  </button>
                </div>
              ) : null}
              {!editingActiveFile ? (
                <button type="button" onClick={() => onBeginEdit(activeTab.path)}>
                  <span className="codicon codicon-edit" aria-hidden="true" />
                  {t("workspace.preview.editFile")}
                </button>
              ) : null}
              {editingActiveFile ? (
                <>
                  {hasUnsavedEdit ? (
                    <span className="workspace-preview-dirty-indicator">{t("workspace.preview.unsaved")}</span>
                  ) : null}
                  <button
                    type="button"
                    disabled={savingPath === activeTab.path}
                    onClick={onCancelEdit}
                  >
                    {t("workspace.preview.cancelEdit")}
                  </button>
                  <button
                    type="button"
                    className="workspace-preview-save-button"
                    disabled={!hasUnsavedEdit || savingPath === activeTab.path}
                    onClick={() => void onSaveEdit()}
                  >
                    <span
                      className={`codicon ${
                        savingPath === activeTab.path
                          ? "codicon-loading codicon-modifier-spin"
                          : "codicon-save"
                      }`}
                      aria-hidden="true"
                    />
                    {savingPath === activeTab.path ? t("workspace.preview.saving") : t("workspace.preview.save")}
                  </button>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
        {activeTab?.previewType === "file" ? (
          <>
            {debugActiveFile && !editingActiveFile ? (
              <div className="workspace-preview-code-scroll workspace-debug-editor-scroll">
                <div className="workspace-preview-code-table workspace-debug-editor-table">
                  {activeEditorLines.map((line, index) => {
                    const lineNumber = index + 1;
                    const breakpoint = debugBreakpointByLine.get(lineNumber) ?? null;
                    const current = lineNumber === activeDebugLine;
                    return (
                      <div
                        ref={current ? debugExecutionLineRef : undefined}
                        className={`workspace-preview-line workspace-debug-editor-line${current ? " current" : ""}`}
                        key={lineNumber}
                      >
                        <NodeDebugBreakpointGutter
                          className="workspace-debug-editor-gutter"
                          path={activeTab.path}
                          line={lineNumber}
                          breakpoint={breakpoint}
                          current={current}
                          disabled={debugActionBusy || !onChangeDebugBreakpoint}
                          onChange={(targetPath, targetLine, breakpointId, definition) => {
                            onChangeDebugBreakpoint?.(
                              targetPath,
                              targetLine,
                              breakpointId,
                              definition,
                            );
                          }}
                        />
                        <span className="workspace-preview-line-number">{lineNumber}</span>
                        <code className="workspace-preview-line-code">
                          {line.length > 0 ? line : "\u00A0"}
                        </code>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : markdownActiveFile && !markdownSourceVisible && !editingActiveFile ? (
              <WorkspaceMarkdownPreview
                apiPort={apiPort}
                workspaceId={workspaceId}
                path={activeTab.path}
                fragment={activeTab.fragment}
                content={activeEditorContent}
                onOpenWorkspacePath={onOpenWorkspacePath}
              />
            ) : <WorkspaceCodePreview
              path={activeTab.path}
              content={activeEditorContent}
              language={activeTab.language}
              readOnly={!editingActiveFile}
              selection={activeTab.selection}
              onBeginEdit={() => onBeginEdit(activeTab.path)}
              onChange={onDraftChange}
              onSave={() => {
                if (hasUnsavedEdit && savingPath !== activeTab.path) void onSaveEdit();
              }}
            />}

          </>
        ) : activeTab?.previewType === "terminal" ? (
          <iframe
            className="workspace-preview-terminal-frame"
            src={activeTab.attachUrl}
            title={t("workspace.runtime.frameTitle", t("workspace.runtime.terminal"), activeTab.terminalId)}
          />
        ) : activeTab?.previewType === "browser" ? (
          <iframe
            className="workspace-preview-terminal-frame workspace-preview-browser-frame"
            src={activeTab.attachUrl}
            title={t("workspace.runtime.frameTitle", t("workspace.runtime.browser"), activeTab.browserId)}
          />
        ) : activeTab?.previewType === "session-diff" ? (
          <div className="workspace-preview-diff-scroll">
              {diffTruncated ? (
                <div className="workspace-preview-truncation-notice" role="status">
                  <span>
                    {t("workspace.preview.diff.bounded", activeDiffProjection.lineCount, MAX_RENDERED_DIFF_LINES)}
                  </span>
                  <button
                    type="button"
                    onClick={() => setDiffFullyExpanded((value) => !value)}
                  >
                    {diffFullyExpanded
                      ? t("workspace.preview.diff.collapse", MAX_RENDERED_DIFF_LINES)
                      : t("workspace.preview.diff.expand", activeDiffProjection.lineCount)}
                  </button>
                </div>
              ) : null}
              <div
                className="workspace-preview-code-table workspace-preview-diff-table"
                style={{ "--preview-line-number-width": `${diffLineNumberWidth}ch` } as CSSProperties}
              >
                {visibleDiffLines.map((line, index) => (
                  <div
                    className={`workspace-preview-line workspace-preview-diff-line diff-${diffLineKind(line)}`}
                    key={`${index}-${line.length}`}
                  >
                    <span className="workspace-preview-line-number">{index + 1}</span>
                    <code className="workspace-preview-line-code">
                      {line.length > 0 ? line : "\u00A0"}
                    </code>
                  </div>
                ))}
              </div>
          </div>
        ) : (
          <div className="workspace-preview-empty">
            <span className="workspace-preview-empty-icon">▱</span>
            <span>{showLoadingEmpty ? t("workspace.preview.loadingFile") : t("workspace.preview.chooseFile")}</span>
          </div>
        )}

        {loadingPath ? (
          <div className="workspace-preview-status">{t("workspace.preview.readingFile", loadingPath)}</div>
        ) : null}
        {error ? (
          <div className="workspace-preview-error failure-summary" role="alert">
            {failureSummary(error)}
          </div>
        ) : null}
      </div>
    </section>
  );
}
