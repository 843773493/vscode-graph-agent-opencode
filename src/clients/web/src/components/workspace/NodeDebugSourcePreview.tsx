import { useEffect, useMemo, useState } from "react";

import { getWorkspaceFileContent } from "../../api";
import type { NodeDebugBreakpoint } from "../../types/backend";
import NodeDebugBreakpointGutter, {
  type NodeDebugBreakpointDefinition,
} from "./NodeDebugBreakpointGutter";

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

export default function NodeDebugSourcePreview({
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
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      disposed = true;
    };
  }, [apiPort, path, sourceRevision, workspaceId]);

  const sourceLines = useMemo(() => content?.split("\n") ?? [], [content]);
  const visibleRange = useMemo(() => {
    if (sourceLines.length === 0) return { start: 0, end: 0 };
    const center = Math.max(1, Math.min(focusLine ?? 1, sourceLines.length));
    return {
      start: Math.max(0, center - 1 - SOURCE_WINDOW_RADIUS),
      end: Math.min(sourceLines.length, center + SOURCE_WINDOW_RADIUS),
    };
  }, [focusLine, sourceLines.length]);
  const breakpointByLine = useMemo(
    () => new Map(
      breakpoints
        .filter((breakpoint) => breakpoint.path === path)
        .map((breakpoint) => [breakpoint.line, breakpoint]),
    ),
    [breakpoints, path],
  );

  if (!path) {
    return <div className="debug-empty-state compact">选择脚本或等待模型命中源码断点。</div>;
  }
  if (error) return <div className="debug-error" role="alert">{error}</div>;
  if (content === null) return <div className="debug-empty-state compact">正在读取 {path}…</div>;

  return (
    <div className="node-debug-source-preview">
      <header>
        <span title={path}>{path}</span>
        <button type="button" onClick={() => void onOpenWorkspacePath(path)} title="在完整编辑器打开">
          <span className="codicon codicon-go-to-file" aria-hidden="true" />
          打开
        </button>
      </header>
      <div className="node-debug-code" role="grid" aria-label={`${path} 源码预览`}>
        {sourceLines.slice(visibleRange.start, visibleRange.end).map((line, index) => {
          const lineNumber = visibleRange.start + index + 1;
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
}
