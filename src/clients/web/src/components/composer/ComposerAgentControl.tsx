import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type { Agent } from "../../types/backend";
import AnchoredOverlay from "../overlays/AnchoredOverlay";

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
  onManage,
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
  onManage?: () => void;
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
        title={t("composer.agent.select", currentAgent)}
        aria-label={t("composer.agent.select", currentAgent)}
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
                    ? t("composer.agent.default")
                    : t("composer.agent.makeDefault")}
                  aria-label={agent.workspace_default
                    ? t("composer.agent.isDefault", agent.name)
                    : t("composer.agent.setDefault", agent.name)}
                  aria-pressed={agent.workspace_default}
                  onClick={() => onSetWorkspaceDefault(agent.agent_id)}
                >
                  <span className="codicon codicon-pin" aria-hidden="true" />
                </button>
              </div>
            ))
          ) : (
            <button type="button" className="composer-agent-menu-item" disabled>
              <span className="composer-agent-menu-label">{t("composer.agent.empty")}</span>
              <span className="composer-agent-menu-description">
                {t("composer.agent.unavailable")}
              </span>
            </button>
          )}
          {onManage ? <button type="button" role="menuitem" className="composer-agent-menu-item" onClick={onManage}>{t('workbench.settings.agents')}</button> : null}
        </div>
      </AnchoredOverlay>
    </div>
  );
}
