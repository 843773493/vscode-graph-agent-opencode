import React, { useEffect, useRef, useState } from 'react';
import { useAppState } from '../hooks';

export type WorkbenchView = "sessions" | "gateway";

function Icon({ children }: { children: React.ReactNode }) {
  return <span className="toolbar-button-icon" aria-hidden="true">{children}</span>;
}

export default function Toolbar({
  sessionTitle,
  onCreateSession,
  auxiliaryVisible,
  onToggleAuxiliaryPanel,
  workbenchView,
  onWorkbenchViewChange,
  showAuxiliaryToggle,
}: {
  sessionTitle: string | null | undefined;
  onCreateSession: () => void;
  auxiliaryVisible: boolean;
  onToggleAuxiliaryPanel: () => void;
  workbenchView: WorkbenchView;
  onWorkbenchViewChange: (view: WorkbenchView) => void;
  showAuxiliaryToggle: boolean;
}) {
  const {
    setStatus,
  } = useAppState();
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const viewMenuRef = useRef<HTMLDivElement | null>(null);
  const titleLabel = sessionTitle?.trim() || '新会话';

  useEffect(() => {
    if (!viewMenuOpen) {
      return;
    }
    const handlePointerDown = (event: PointerEvent) => {
      if (!viewMenuRef.current?.contains(event.target as Node)) {
        setViewMenuOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setViewMenuOpen(false);
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [viewMenuOpen]);

  const selectWorkbenchView = (view: WorkbenchView) => {
    setViewMenuOpen(false);
    onWorkbenchViewChange(view);
  };

  return (
    <header className="toolbar">
      <div className="toolbar-group toolbar-group-left">
        <button type="button" className="toolbar-icon-button" title="Web 端暂无导航历史" aria-label="后退" disabled>
          ‹
        </button>
        <button type="button" className="toolbar-icon-button" title="Web 端暂无导航历史" aria-label="前进" disabled>
          ›
        </button>
        <button
          type="button"
          className="toolbar-icon-button toolbar-icon-primary"
          title="新建会话"
          onClick={() => onCreateSession()}
        >
          <Icon>
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>
          </Icon>
        </button>
        <div className="workbench-view-switcher" ref={viewMenuRef}>
          <button
            type="button"
            className={`toolbar-view-button${viewMenuOpen ? " active" : ""}`}
            title="切换工作台视图"
            aria-label="工作台视图"
            aria-haspopup="menu"
            aria-expanded={viewMenuOpen}
            onClick={() => setViewMenuOpen((open) => !open)}
          >
            <span className="codicon codicon-layout" aria-hidden="true" />
            <span>视图</span>
            <span className="codicon codicon-chevron-down" aria-hidden="true" />
          </button>
          {viewMenuOpen ? (
            <div className="workbench-view-menu" role="menu" aria-label="工作台视图">
              <button
                type="button"
                role="menuitemradio"
                aria-checked={workbenchView === "sessions"}
                className={workbenchView === "sessions" ? "active" : ""}
                onClick={() => selectWorkbenchView("sessions")}
              >
                <span className="codicon codicon-comment-discussion" aria-hidden="true" />
                <span className="workbench-view-menu-copy">
                  <strong>会话工作台</strong>
                  <small>会话、编辑器与文件变更</small>
                </span>
                {workbenchView === "sessions" ? (
                  <span className="codicon codicon-check" aria-hidden="true" />
                ) : null}
              </button>
              <button
                type="button"
                role="menuitemradio"
                aria-checked={workbenchView === "gateway"}
                className={workbenchView === "gateway" ? "active" : ""}
                onClick={() => selectWorkbenchView("gateway")}
              >
                <span className="codicon codicon-server-environment" aria-hidden="true" />
                <span className="workbench-view-menu-copy">
                  <strong>Gateway 控制台</strong>
                  <small>管理工作区、连接与路由</small>
                </span>
                {workbenchView === "gateway" ? (
                  <span className="codicon codicon-check" aria-hidden="true" />
                ) : null}
              </button>
            </div>
          ) : null}
        </div>
      </div>
      <div className="toolbar-center" title={titleLabel}>
        <div className="command-center">
          <span className="command-center-icon" aria-hidden="true">▱</span>
          <span className="command-center-title">
            {titleLabel}
          </span>
        </div>
      </div>
      <div className="toolbar-group toolbar-group-right">
        <button
          type="button"
          className="toolbar-update-button"
          title="检查更新"
          onClick={() => setStatus("Web UI 已是当前本地构建")}
        >
          更新
        </button>
        {showAuxiliaryToggle ? <button
          type="button"
          className={`toolbar-icon-button titlebar-auxiliary-button${auxiliaryVisible ? " active" : ""}`}
          title={auxiliaryVisible ? "隐藏右侧侧边栏" : "显示右侧侧边栏"}
          aria-label={auxiliaryVisible ? "隐藏右侧侧边栏" : "显示右侧侧边栏"}
          aria-pressed={auxiliaryVisible}
          onClick={onToggleAuxiliaryPanel}
        >
          <span
            className={`codicon ${auxiliaryVisible ? "codicon-layout-sidebar-right" : "codicon-layout-sidebar-right-off"}`}
            aria-hidden="true"
          />
        </button> : null}
      </div>
    </header>
  );
}
