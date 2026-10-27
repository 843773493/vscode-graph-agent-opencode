import type { WebUiBottomPanelTab } from "../../../types/backend";

const tabs = [
  ["terminal", "terminal", "终端"], ["output", "output", "输出"],
  ["ports", "server-environment", "端口"], ["automation", "gear", "自动化"], ["mcp", "plug", "MCP 连接"],
] as const;

export default function BottomPanelTabs({ selected, onSelect, prefix, terminalCount }: {
  selected: WebUiBottomPanelTab;
  onSelect: (tab: WebUiBottomPanelTab) => void;
  prefix: string;
  terminalCount?: number;
}) {
  return <div className={`${prefix}-tabs`} role="tablist" aria-label="底部面板">
    {tabs.map(([tab, icon, label]) => <button key={tab} type="button" className={`${prefix}-tab${selected === tab ? " active" : ""}`}
      role="tab" aria-selected={selected === tab} onClick={() => onSelect(tab)}>
      <span className={`codicon codicon-${icon}`} aria-hidden="true" /><span>{label}</span>
      {tab === "terminal" && terminalCount !== undefined ? <span className="terminal-panel-count">{terminalCount}</span> : null}
    </button>)}
  </div>;
}
