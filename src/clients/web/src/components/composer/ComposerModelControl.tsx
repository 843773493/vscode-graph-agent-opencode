import { t } from "../../../../shared/core/i18n/index.js";
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
          ? t("composer.model.unavailable")
          : currentConfigurationError
            ? t("composer.model.currentError", currentConfigurationError)
          : t("composer.model.selectWithId", label, currentProviderId)}
        aria-label={currentConfigurationError
          ? t("composer.model.selectError", label, currentConfigurationError)
          : t("composer.model.select", label)}
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
                    ? t("composer.model.namedError", provider.model, configurationError)
                    : `${provider.model}，${provider.provider_id} · ${provider.custom_llm_provider}`}
                  title={configurationError
                    ? t("composer.model.error", configurationError)
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
                      {t("composer.model.errorDetail", configurationError)}
                    </span>
                  ) : null}
                </button>
                <button
                  type="button"
                  className="composer-workspace-default-button"
                  title={provider.workspace_default
                    ? t("composer.model.default")
                    : t("composer.model.makeDefault")}
                  aria-label={provider.workspace_default
                    ? t("composer.model.isDefault", provider.model)
                    : t("composer.model.setDefault", provider.model)}
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
