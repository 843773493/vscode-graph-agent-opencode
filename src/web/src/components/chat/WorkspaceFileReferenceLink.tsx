import React, { useEffect, useState } from "react";
import {
  useWorkspaceFileReferenceContext,
  type WorkspaceFileReferenceResolution,
} from "../workspace/WorkspaceFileReferenceContext";

export default function WorkspaceFileReferenceLink({
  target,
  children,
  inlineCode = false,
}: {
  target: string;
  children: React.ReactNode;
  inlineCode?: boolean;
}) {
  const context = useWorkspaceFileReferenceContext();
  const [resolution, setResolution] = useState<WorkspaceFileReferenceResolution | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    setResolution(null);
    if (!context) {
      return () => {
        cancelled = true;
      };
    }
    void context.resolve(target).then((nextResolution) => {
      if (!cancelled) {
        setResolution(nextResolution);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [context, target]);

  if (resolution?.status === "resolved" && context) {
    const { reference } = resolution;
    const lineLabel = reference.selection
      ? reference.selection.startLine === reference.selection.endLine
        ? `，第 ${reference.selection.startLine} 行`
        : `，第 ${reference.selection.startLine}-${reference.selection.endLine} 行`
      : "";
    return (
      <button
        type="button"
        className={`chat-file-reference${inlineCode ? " inline-code" : ""}`}
        title={`在预览区打开 ${reference.path}${lineLabel}`}
        aria-label={`在预览区打开文件 ${reference.path}${lineLabel}`}
        onClick={() => context.open(resolution)}
      >
        <span className="codicon codicon-file" aria-hidden="true" />
        <span>{children}</span>
      </button>
    );
  }

  const errorTitle = resolution?.status === "error"
    ? `文件引用验证失败：${resolution.message}`
    : undefined;
  return inlineCode ? (
    <code title={errorTitle}>{children}</code>
  ) : (
    <span className="chat-file-reference-fallback" title={errorTitle}>
      {children}
    </span>
  );
}
