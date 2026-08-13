import React from "react";
import type { Agent } from "../../types/backend";
import AnchoredOverlay from "../AnchoredOverlay";

export default function ComposerAgentControl({
  controlRef,
  agents,
  currentAgent,
  open,
  onToggle,
  onClose,
  onSelect,
  onSetWorkspaceDefault,
  onKeyDown,
}: {
  controlRef: React.RefObject<HTMLDivElement>;
  agents: Agent[];
  currentAgent: string;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onSelect: (agentId: string) => void;
  onSetWorkspaceDefault: (agentId: string) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
}): React.ReactNode {
  return (
    <div
      ref={controlRef}
      className="composer-agent-control"
      onKeyDown={onKeyDown}
    >
      <button
        id="agentSelectButton"
        type="button"
        className="composer-agent-button"
        title={`选择 Agent，当前：${currentAgent}`}
        aria-label={`选择 Agent，当前：${currentAgent}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span
          className="codicon codicon-sparkle composer-picker-button-icon"
          aria-hidden="true"
        />
        <span className="composer-agent-label">{currentAgent}</span>
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={controlRef}
        placement="top-end"
        onClose={onClose}
      >
        <div className="composer-agent-menu" role="menu">
          {agents.length > 0 ? (
            agents.map((agent) => (
              <div
                key={agent.agent_id}
                className={`composer-agent-menu-item${
                  agent.agent_id === currentAgent ? " active" : ""
                }${agent.workspace_default ? " workspace-default" : ""}`}
              >
                <button
                  type="button"
                  className="composer-menu-item-main"
                  role="menuitemradio"
                  aria-checked={agent.agent_id === currentAgent}
                  title={agent.description ?? agent.name}
                  onClick={() => onSelect(agent.agent_id)}
                >
                  <span className="composer-agent-menu-label">
                    {agent.name || agent.agent_id}
                  </span>
                  <span className="composer-agent-menu-description">
                    {agent.agent_id}
                    {agent.description ? ` · ${agent.description}` : ""}
                  </span>
                </button>
                <button
                  type="button"
                  className="composer-workspace-default-button"
                  title={agent.workspace_default
                    ? "当前工作区默认 Agent"
                    : "设为工作区默认 Agent，仅影响新会话"}
                  aria-label={agent.workspace_default
                    ? `${agent.name} 已是工作区默认 Agent`
                    : `将 ${agent.name} 设为工作区默认 Agent`}
                  aria-pressed={agent.workspace_default}
                  onClick={() => onSetWorkspaceDefault(agent.agent_id)}
                >
                  <span className="codicon codicon-pin" aria-hidden="true" />
                </button>
              </div>
            ))
          ) : (
            <button type="button" className="composer-agent-menu-item" disabled>
              <span className="composer-agent-menu-label">暂无 Agent</span>
              <span className="composer-agent-menu-description">
                后端未返回可切换的 Agent
              </span>
            </button>
          )}
        </div>
      </AnchoredOverlay>
    </div>
  );
}
