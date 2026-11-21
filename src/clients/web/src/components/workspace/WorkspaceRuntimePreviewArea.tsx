import { useEffect, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import type { WorkspacePreviewTab } from "./WorkspaceFilePreviewArea";
import { useTerminalFrameObservation } from "../../hooks/workspace/useTerminalFrameObservation";

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
  const frame = useRef<HTMLIFrameElement>(null);
  useTerminalFrameObservation(frame, expanded && tab?.previewType === "terminal" ? tab.attachUrl : null);

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
      aria-label={extensionWindow ? t("workspace.runtime.extensionWindow") : t("workspace.runtime.preview")}
      data-extension-region={extensionWindow ? "primary" : undefined}
    >
      {!extensionWindow ? <header className="workspace-runtime-preview-header">
        {onExitExtensionWindow ? (
          <button
            type="button"
            className="workspace-runtime-preview-close"
            title={t("workspace.runtime.returnToStandardWindow")}
            aria-label={t("workspace.runtime.returnToStandardWindow")}
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
          <strong>{extensionWindow ? t("workspace.runtime.extensionWindow") : isBrowser ? t("workspace.runtime.browser") : t("workspace.runtime.terminal")}</strong>
          <span title={tab.scopeLabel ?? tab.path}>
            {extensionWindow
              ? `${isBrowser ? t("workspace.runtime.browser") : t("workspace.runtime.terminal")} · ${tab.name}`
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
          title={t("workspace.runtime.closePreview")}
          aria-label={t("workspace.runtime.closePreview")}
          onClick={() => void onClose()}
        >
          <span className="codicon codicon-close" aria-hidden="true" />
        </button>
      </header> : null}
      {expanded ? (
        <iframe
          ref={frame}
          className={`workspace-runtime-preview-frame${isBrowser ? " browser" : " terminal"}`}
          src={tab.attachUrl}
          title={`${isBrowser ? t("workspace.runtime.browser") : t("workspace.runtime.terminal")} ${tab.name}`}
        />
      ) : null}
    </section>
  );
}
