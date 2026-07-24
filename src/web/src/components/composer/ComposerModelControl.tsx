import React from "react";
import type { Agent } from "../../types/backend";
import AnchoredOverlay from "../AnchoredOverlay";

type AgentProvider = Agent["providers"][number];

export default function ComposerModelControl({
  controlRef,
  providers,
  currentProviderId,
  open,
  disabled,
  onToggle,
  onClose,
  onSelect,
  onSetWorkspaceDefault,
  onKeyDown,
}: {
  controlRef: React.RefObject<HTMLDivElement>;
  providers: AgentProvider[];
  currentProviderId: string;
  open: boolean;
  disabled: boolean;
  onToggle: () => void;
  onClose: () => void;
  onSelect: (providerId: string) => void;
  onSetWorkspaceDefault: (providerId: string) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
}): React.ReactNode {
  const current = providers.find(
    (provider) => provider.provider_id === currentProviderId,
  );
  const label = current?.model ?? currentProviderId;

  return (
    <div
      ref={controlRef}
      className="composer-model-control"
      onKeyDown={onKeyDown}
    >
      <button
        type="button"
        className="composer-model-pill"
        title={disabled
          ? "当前 Agent 没有可选模型"
          : `选择模型，当前：${label}（${currentProviderId}）`}
        aria-label={`选择模型，当前：${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={onToggle}
      >
        <span
          className="codicon codicon-chip composer-picker-button-icon"
          aria-hidden="true"
        />
        <span className="composer-model-label">{label}</span>
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={controlRef}
        placement="top-end"
        onClose={onClose}
      >
        <div className="composer-model-menu" role="menu">
          {providers.map((provider) => (
            <div
              key={provider.provider_id}
              className={`composer-model-menu-item${
                provider.provider_id === currentProviderId ? " active" : ""
              }${provider.workspace_default ? " workspace-default" : ""}`}
            >
              <button
                type="button"
                className="composer-menu-item-main"
                role="menuitemradio"
                aria-checked={provider.provider_id === currentProviderId}
                onClick={() => onSelect(provider.provider_id)}
              >
                <span className="composer-model-menu-label">{provider.model}</span>
                <span className="composer-model-menu-description">
                  {provider.provider_id} · {provider.custom_llm_provider}
                </span>
              </button>
              <button
                type="button"
                className="composer-workspace-default-button"
                title={provider.workspace_default
                  ? "当前工作区默认模型"
                  : "设为工作区默认模型，仅影响新会话"}
                aria-label={provider.workspace_default
                  ? `${provider.model} 已是工作区默认模型`
                  : `将 ${provider.model} 设为工作区默认模型`}
                aria-pressed={provider.workspace_default}
                onClick={() => onSetWorkspaceDefault(provider.provider_id)}
              >
                <span className="codicon codicon-pin" aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      </AnchoredOverlay>
    </div>
  );
}
