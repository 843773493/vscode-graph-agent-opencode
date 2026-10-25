import { memo, useEffect, useMemo, useState } from "react";
import { errorMessage } from "../../utils/errorMessage";

import { getWorkspaceFileContent } from "../../api";
import type { NodeDebugBreakpoint } from "../../types/backend";
import NodeDebugBreakpointGutter, {
  type NodeDebugBreakpointDefinition,
} from "./NodeDebugBreakpointGutter";
import { createSourceLineStarts, visibleSourceLines } from "./nodeDebugSourceLines";

interface NodeDebugSourcePreviewProps {
  apiPort: number;
  workspaceId: string | null;
  path: string | null;
  focusLine: number | null;
  sourceRevision: number;
  breakpoints: NodeDebugBreakpoint[];
  disabled: boolean;
  onChangeBreakpoint: (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}

const SOURCE_WINDOW_RADIUS = 7;

const NodeDebugSourcePreview = memo(function NodeDebugSourcePreview({
  apiPort,
  workspaceId,
  path,
  focusLine,
  sourceRevision,
  breakpoints,
  disabled,
  onChangeBreakpoint,
  onOpenWorkspacePath,
}: NodeDebugSourcePreviewProps) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    setContent(null);
    setError(null);
    if (!path) return;
    void getWorkspaceFileContent(apiPort, path, workspaceId)
      .then((file) => {
        if (!disposed) setContent(file.content);
      })
      .catch((cause: unknown) => {
        if (!disposed) setError(errorMessage(cause));
      });
    return () => {
      disposed = true;
    };
  }, [apiPort, path, sourceRevision, workspaceId]);

  const lineStartOffsets = useMemo(
    () => content === null ? [] : createSourceLineStarts(content),
    [content],
  );
  const visibleLines = useMemo(
    () => content === null
      ? []
      : visibleSourceLines(content, lineStartOffsets, focusLine, SOURCE_WINDOW_RADIUS),
    [content, focusLine, lineStartOffsets],
  );
  const breakpointByLine = useMemo(
    () => new Map(
      breakpoints
        .filter((breakpoint) => breakpoint.path === path)
        .map((breakpoint) => [breakpoint.line, breakpoint]),
    ),
    [breakpoints, path],
  );

  if (!path) {
    return null;
  }
  return (
    <div className="node-debug-source-preview">
      <header>
        <span title={path}>{path}</span>
        {content !== null && !error && (
          <button type="button" onClick={() => void onOpenWorkspacePath(path)} title="在完整编辑器打开">
            <span className="codicon codicon-go-to-file" aria-hidden="true" />
            打开
          </button>
        )}
      </header>
      <div
        className="node-debug-code"
        role={error ? "alert" : content === null ? "status" : "grid"}
        aria-label={`${path} 源码预览`}
      >
        {error ? (
          <div className="node-debug-source-state error">{error}</div>
        ) : content === null ? (
          <div className="node-debug-source-state">正在读取 {path}…</div>
        ) : visibleLines.map(({ line, lineNumber }) => {
          const breakpoint = breakpointByLine.get(lineNumber) ?? null;
          const current = lineNumber === focusLine;
          return (
            <div
              className={`node-debug-code-line${current ? " current" : ""}`}
              role="row"
              key={lineNumber}
            >
              <NodeDebugBreakpointGutter
                className="node-debug-gutter"
                path={path}
                line={lineNumber}
                breakpoint={breakpoint}
                current={current}
                disabled={disabled}
                onChange={onChangeBreakpoint}
              />
              <span className="node-debug-line-number" aria-hidden="true">{lineNumber}</span>
              <code>{line || " "}</code>
            </div>
          );
        })}
      </div>
      <small>源码会跟随模型或用户的当前暂停位置；左键切换普通断点，右键设置条件、命中次数或日志点。</small>
    </div>
  );
});

export default NodeDebugSourcePreview;
