import React from "react";
import type { Agent } from "../types/backend";

export default function ComposerAgentControl({
  controlRef,
  agents,
  currentAgent,
  open,
  onToggle,
  onSelect,
  onKeyDown,
}: {
  controlRef: React.RefObject<HTMLDivElement>;
  agents: Agent[];
  currentAgent: string;
  open: boolean;
  onToggle: () => void;
  onSelect: (agentId: string) => void;
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
        <span className="composer-agent-label">{currentAgent}</span>
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4z" />
        </svg>
      </button>
      {open && (
        <div className="composer-agent-menu" role="menu">
          {agents.length > 0 ? (
            agents.map((agent) => (
              <button
                key={agent.agent_id}
                type="button"
                className={`composer-agent-menu-item${
                  agent.agent_id === currentAgent ? " active" : ""
                }`}
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
      )}
    </div>
  );
}
