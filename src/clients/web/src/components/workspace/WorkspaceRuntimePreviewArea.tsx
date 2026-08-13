import { useEffect, useState } from "react";
import type { WorkspacePreviewTab } from "./WorkspaceFilePreviewArea";

export type WorkspaceRuntimePreviewTab = Extract<
  WorkspacePreviewTab,
  { previewType: "terminal" | "browser" }
>;

interface WorkspaceRuntimePreviewAreaProps {
  tab: WorkspaceRuntimePreviewTab | null;
  onClose: () => Promise<void>;
  extensionWindow?: boolean;
  onExitExtensionWindow?: () => void;
}

export default function WorkspaceRuntimePreviewArea({
  tab,
  onClose,
  extensionWindow = false,
  onExitExtensionWindow,
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
      aria-label={extensionWindow ? "扩展窗口" : "运行时预览"}
      data-extension-region={extensionWindow ? "primary" : undefined}
    >
      <header className="workspace-runtime-preview-header">
        {onExitExtensionWindow ? (
          <button
            type="button"
            className="workspace-runtime-preview-close"
            title="返回标准窗口"
            aria-label="返回标准窗口"
            onClick={onExitExtensionWindow}
          >
            <span className="codicon codicon-chevron-left" aria-hidden="true" />
          </button>
        ) : null}
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
          <strong>{extensionWindow ? "扩展窗口" : isBrowser ? "浏览器" : "终端"}</strong>
          <span title={tab.scopeLabel ?? tab.path}>
            {extensionWindow
              ? `${isBrowser ? "浏览器" : "终端"} · ${tab.name}`
              : tab.name}
          </span>
          {extensionWindow && tab.scopeLabel ? (
            <small title={tab.scopeLabel}>{tab.scopeLabel}</small>
          ) : null}
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
