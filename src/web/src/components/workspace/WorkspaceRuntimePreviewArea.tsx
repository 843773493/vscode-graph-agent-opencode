import { useEffect, useState } from "react";
import type { WorkspacePreviewTab } from "./WorkspaceFilePreviewArea";

export type WorkspaceRuntimePreviewTab = Extract<
  WorkspacePreviewTab,
  { previewType: "terminal" | "browser" }
>;

interface WorkspaceRuntimePreviewAreaProps {
  tab: WorkspaceRuntimePreviewTab | null;
  onClose: () => Promise<void>;
}

export default function WorkspaceRuntimePreviewArea({
  tab,
  onClose,
}: WorkspaceRuntimePreviewAreaProps) {
  const [expanded, setExpanded] = useState(true);

  useEffect(() => {
    setExpanded(true);
  }, [tab?.path]);

  if (!tab) {
    return null;
  }

  const isBrowser = tab.previewType === "browser";
  return (
    <section
      className={`workspace-runtime-preview${expanded ? " expanded" : " collapsed"}`}
      aria-label="运行时预览"
    >
      <header className="workspace-runtime-preview-header">
        <button
          type="button"
          className="workspace-runtime-preview-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span
            className={`codicon codicon-chevron-${expanded ? "down" : "right"}`}
            aria-hidden="true"
          />
          <span className="workspace-runtime-preview-copy">
          <strong>{isBrowser ? "浏览器" : "终端"}</strong>
          <span title={tab.path}>{tab.name}</span>
          </span>
        </button>
        <button
          type="button"
          className="workspace-runtime-preview-close"
          title="关闭运行时预览"
          aria-label="关闭运行时预览"
          onClick={() => void onClose()}
        >
          <span className="codicon codicon-close" aria-hidden="true" />
        </button>
      </header>
      {expanded ? (
        <iframe
          className={`workspace-runtime-preview-frame${isBrowser ? " browser" : " terminal"}`}
          src={tab.attachUrl}
          title={`${isBrowser ? "浏览器" : "终端"} ${tab.name}`}
        />
      ) : null}
    </section>
  );
}
