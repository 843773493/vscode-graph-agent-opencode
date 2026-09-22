import React from "react";
import type { Agent } from "../../types/backend";
import AnchoredOverlay from "../overlays/AnchoredOverlay";

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
  const currentConfigurationError = current?.configuration_error?.trim();

  return (
    <div
      ref={controlRef}
      className="composer-model-control"
      onKeyDown={onKeyDown}
    >
      <button
        type="button"
        className={`composer-model-pill${currentConfigurationError ? " configuration-error" : ""}`}
        title={disabled
          ? "当前 Agent 没有可选模型"
          : currentConfigurationError
            ? `当前模型配置错误：${currentConfigurationError}`
          : `选择模型，当前：${label}（${currentProviderId}）`}
        aria-label={currentConfigurationError
          ? `选择模型，当前 ${label} 配置错误：${currentConfigurationError}`
          : `选择模型，当前：${label}`}
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
        {currentConfigurationError ? (
          <span className="codicon codicon-error" aria-hidden="true" />
        ) : null}
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={controlRef}
        placement="top-end"
        onClose={onClose}
      >
        <div className="composer-model-menu" role="menu">
          {providers.map((provider) => {
            const configurationError = provider.configuration_error?.trim();
            const available = provider.available && !configurationError;
            return (
              <div
                key={provider.provider_id}
                className={`composer-model-menu-item${
                  provider.provider_id === currentProviderId ? " active" : ""
                }${provider.workspace_default ? " workspace-default" : ""}${
                  available ? "" : " configuration-error"
                }`}
              >
                <button
                  type="button"
                  className="composer-menu-item-main"
                  role="menuitemradio"
                  aria-checked={provider.provider_id === currentProviderId}
                  aria-label={configurationError
                    ? `${provider.model} 配置错误：${configurationError}`
                    : `${provider.model}，${provider.provider_id} · ${provider.custom_llm_provider}`}
                  title={configurationError
                    ? `模型配置错误：${configurationError}`
                    : undefined}
                  disabled={!available}
                  onClick={() => onSelect(provider.provider_id)}
                >
                  <span className="composer-model-menu-label">{provider.model}</span>
                  <span className="composer-model-menu-description">
                    {provider.provider_id} · {provider.custom_llm_provider}
                  </span>
                  {configurationError ? (
                    <span className="composer-model-menu-error" role="alert">
                      <span className="codicon codicon-error" aria-hidden="true" />
                      配置错误：{configurationError}
                    </span>
                  ) : null}
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
                  disabled={!available}
                  onClick={() => onSetWorkspaceDefault(provider.provider_id)}
                >
                  <span className="codicon codicon-pin" aria-hidden="true" />
                </button>
              </div>
            );
          })}
        </div>
      </AnchoredOverlay>
    </div>
  );
}
