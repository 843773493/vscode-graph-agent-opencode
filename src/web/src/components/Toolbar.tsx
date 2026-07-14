import React from 'react';
import { useAppState } from '../hooks';

function Icon({ children }: { children: React.ReactNode }) {
  return <span className="toolbar-button-icon" aria-hidden="true">{children}</span>;
}

export default function Toolbar({
  sessionTitle,
  onCreateSession,
  auxiliaryVisible,
  onToggleAuxiliaryPanel,
}: {
  sessionTitle: string | null | undefined;
  onCreateSession: () => void;
  auxiliaryVisible: boolean;
  onToggleAuxiliaryPanel: () => void;
}) {
  const {
    setStatus,
  } = useAppState();
  const titleLabel = sessionTitle?.trim() || '新会话';

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
        <button
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
        </button>
      </div>
    </header>
  );
}
