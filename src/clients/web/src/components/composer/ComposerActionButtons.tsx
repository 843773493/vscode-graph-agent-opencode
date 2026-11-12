import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type { DeliveryPolicy } from "../../types/backend";
import { DELIVERY_POLICY_LABELS } from "../deliveryPolicyPresentation";

export default function ComposerActionButtons({
  hasContent,
  showInterrupt,
  onClear,
  onInterrupt,
  onSend,
  onAlternate,
  onToggleDefault,
  defaultDeliveryPolicy,
}: {
  hasContent: boolean;
  showInterrupt: boolean;
  onClear: () => void;
  onInterrupt: () => void;
  onSend: () => void;
  onAlternate: () => void;
  onToggleDefault: () => void;
  defaultDeliveryPolicy: DeliveryPolicy;
}): React.ReactNode {
  return (
    <>
      {hasContent && (
        <button
          id="clearInputButton"
          type="button"
          title={t("composer.actions.clear")}
          className="composer-icon-button hover-only"
          onClick={onClear}
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path
              d="M3 3l10 10M13 3L3 13"
              stroke="currentColor"
              strokeWidth="1.2"
              strokeLinecap="round"
              strokeLinejoin="round"
              fill="none"
            />
          </svg>
        </button>
      )}
      {showInterrupt && (
        <button
          id="interruptButton"
          type="button"
          className="composer-icon-button interrupt-button"
          onClick={onInterrupt}
          title={t("composer.actions.interrupt")}
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <rect
              x="3"
              y="3"
              width="10"
              height="10"
              rx="2"
              fill="currentColor"
            />
          </svg>
        </button>
      )}
      <button
        id="sendButton"
        data-testid="composer-send"
        type="button"
        className="send-button"
        disabled={!hasContent}
        onClick={onSend}
        title={hasContent ? t("composer.actions.send") : t("composer.actions.emptySend")}
        aria-label={hasContent ? t("composer.actions.send") : t("composer.actions.emptySend")}
      >
        <svg viewBox="0 0 16 16" width="12" height="12">
          <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" />
        </svg>
      </button>
      {showInterrupt && (
        <button
          type="button"
          className="composer-icon-button queue-message-button"
          disabled={!hasContent}
          onClick={onAlternate}
          title={t("composer.actions.alternate")}
          aria-label={t("composer.actions.alternate")}
        >
          <span
            className="codicon codicon-list-ordered"
            aria-hidden="true"
          />
        </button>
      )}
      {showInterrupt && (
        <button
          type="button"
          className="composer-icon-button hover-only"
          onClick={onToggleDefault}
          title={t("composer.actions.default", DELIVERY_POLICY_LABELS[defaultDeliveryPolicy])}
          aria-label={t("composer.actions.changeDefault", DELIVERY_POLICY_LABELS[defaultDeliveryPolicy])}
        >
          <span className="codicon codicon-settings-gear" aria-hidden="true" />
        </button>
      )}
    </>
  );
}
