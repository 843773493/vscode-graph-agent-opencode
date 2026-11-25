import { failureSummary } from "../../../../shared/core/failurePresentation";
import { t } from "../../../../shared/core/i18n/index.js";
import React, { useEffect, useRef, useState } from "react";
import { useWorkspaceFileReferenceContext, type WorkspaceFileReferenceResolution } from "../workspace/WorkspaceFileReferenceContext";
import { isLikelyWorkspaceFileReference, shouldResolveWorkspaceFileReference } from "../../utils/workspaceFileReferences";
import { errorMessage } from "../../utils/errorMessage";

/** 展示链接不读取文件；只有用户打开时才定位目标及读取正文。 */
export default function WorkspaceFileReferenceLink({ target, children, inlineCode = false }: {
  target: string; children: React.ReactNode; inlineCode?: boolean;
}) {
  const context = useWorkspaceFileReferenceContext();
  const [resolution, setResolution] = useState<WorkspaceFileReferenceResolution | null>(null);
  const [opening, setOpening] = useState(false);
  const lifetime = useRef(0);
  useEffect(() => {
    setResolution(null); setOpening(false);
    return () => { lifetime.current++; };
  }, [context?.resolve, target]);
  const open = async () => {
    if (!context || opening) return;
    const generation = lifetime.current;
    setOpening(true);
    try {
      const result = await context.resolve(target);
      if (generation !== lifetime.current) return;
      setResolution(result);
      if (result.status === "resolved") context.open(result);
    } catch (error) {
      if (generation === lifetime.current) setResolution({ status: "error", message: errorMessage(error) });
    } finally { if (generation === lifetime.current) setOpening(false); }
  };
  const errorTitle = resolution?.status === "error" ? failureSummary(t("chat.file.validationFailed", resolution.message)) : undefined;
  if (context && isLikelyWorkspaceFileReference(target) && shouldResolveWorkspaceFileReference(target) && resolution?.status !== "missing") {
    return <button type="button" className={`chat-file-reference${inlineCode ? " inline-code" : ""}`}
      title={errorTitle ?? t("chat.file.open", target, "")} aria-label={t("chat.file.openAria", target, "")}
      aria-busy={opening} disabled={opening} onClick={() => void open()}>
      <span className="codicon codicon-file" aria-hidden="true" /><span>{children}</span>
    </button>;
  }
  return inlineCode ? <code title={errorTitle}>{children}</code>
    : <span className="chat-file-reference-fallback" title={errorTitle}>{children}</span>;
}
