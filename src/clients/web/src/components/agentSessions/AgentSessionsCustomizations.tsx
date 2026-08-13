export const CUSTOMIZATIONS_DEFAULT_HEIGHT = 286;
export const CUSTOMIZATIONS_MIN_HEIGHT = 129;
export const CUSTOMIZATIONS_MAX_HEIGHT = 420;
export const CUSTOMIZATIONS_COLLAPSED_HEIGHT = 36;
export const CUSTOMIZATIONS_RESIZING_CLASS = 'is-customizations-resizing';

export function clampCustomizationsHeight(value: number): number {
  return Math.min(CUSTOMIZATIONS_MAX_HEIGHT, Math.max(CUSTOMIZATIONS_MIN_HEIGHT, value));
}

interface AgentSessionsCustomizationsProps {
  collapsed: boolean;
  height: number;
  sessionCount: number;
  notice: string;
  onCollapsedChange: (collapsed: boolean) => void;
  onShowNotice: (label: string) => void;
}

export default function AgentSessionsCustomizations({
  collapsed,
  height,
  sessionCount,
  notice,
  onCollapsedChange,
  onShowNotice,
}: AgentSessionsCustomizationsProps) {
  return (
    <footer
      className={`sessions-customizations${collapsed ? ' collapsed' : ''}`}
      style={{ flexBasis: height, height }}
    >
      <button
        type="button"
        className={`customizations-header${collapsed ? ' collapsed' : ''}`}
        aria-expanded={!collapsed}
        onClick={() => onCollapsedChange(!collapsed)}
      >
        <span className="customizations-title">自定义</span>
        <span
          className={`codicon ${
            collapsed ? 'codicon-chevron-right' : 'codicon-chevron-down'
          } customizations-chevron`}
          aria-hidden="true"
        />
      </button>
      {!collapsed ? (
        <div className="customizations-body">
          <button type="button" className="customization-link" onClick={() => onShowNotice('概述')}><span className="codicon codicon-home customization-icon" aria-hidden="true" /><span>概述</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('智能体')}><span className="codicon codicon-robot customization-icon" aria-hidden="true" /><span>智能体</span><span className="customization-count">{String(Math.max(0, sessionCount ? 1 : 0))}</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('技能')}><span className="codicon codicon-lightbulb-sparkle customization-icon" aria-hidden="true" /><span>技能</span><span className="customization-count">24</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('指令')}><span className="codicon codicon-terminal customization-icon" aria-hidden="true" /><span>指令</span><span className="customization-count">1</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('挂钩')}><span className="codicon codicon-zap customization-icon" aria-hidden="true" /><span>挂钩</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('MCP 服务器')}><span className="codicon codicon-server-process customization-icon" aria-hidden="true" /><span>MCP 服务器</span><span className="customization-count">1</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('插件')}><span className="codicon codicon-extensions customization-icon" aria-hidden="true" /><span>插件</span></button>
          <button type="button" className="customization-link" onClick={() => onShowNotice('工具')}><span className="codicon codicon-tools customization-icon" aria-hidden="true" /><span>工具</span></button>
          {notice ? (
            <div className="customization-notice" role="status">
              {notice}
            </div>
          ) : null}
        </div>
      ) : null}
    </footer>
  );
}
