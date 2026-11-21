import { t } from "../../../../shared/core/i18n/index.js";
import React, { useRef, useState } from "react";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import type { DeliveryPolicy } from "../../types/backend";
import { DELIVERY_POLICY_LABELS } from "../deliveryPolicyPresentation";

export default function ComposerActionButtons({
  hasContent,
  sendBlocked = false,
  showInterrupt,
  stopPending = false,
  onClear,
  onInterrupt,
  onSend,
  onAlternate,
  onToggleDefault,
  defaultDeliveryPolicy,
}: {
  hasContent: boolean;
  sendBlocked?: boolean;
  showInterrupt: boolean;
  stopPending?: boolean;
  onClear: () => void;
  onInterrupt: () => void;
  onSend: () => void;
  onAlternate: () => void;
  onToggleDefault: () => void;
  defaultDeliveryPolicy: DeliveryPolicy;
}): React.ReactNode {
  const moreRef = useRef<HTMLDivElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  return (
    <>
      {(hasContent || showInterrupt) && (
        <div ref={moreRef} className="composer-more-control">
          <button type="button" className="composer-icon-button" aria-label={t('composer.actions.more')}
            title={showInterrupt ? `${t('composer.actions.more')}\n${t('composer.actions.default', DELIVERY_POLICY_LABELS[defaultDeliveryPolicy])}` : t('composer.actions.more')}
            aria-haspopup="menu" aria-expanded={moreOpen} onClick={() => setMoreOpen(!moreOpen)}>
            <span className="codicon codicon-ellipsis" aria-hidden="true" />
          </button>
          <AnchoredOverlay open={moreOpen} anchorRef={moreRef} placement="top-end" onClose={() => setMoreOpen(false)}>
            <div className="composer-view-menu" role="menu" onKeyDown={event => {
              if (event.key === 'Escape') {
                event.preventDefault(); event.stopPropagation(); setMoreOpen(false);
                moreRef.current?.querySelector('button')?.focus();
                return;
              }
              if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
              const index = items.findIndex(item => item.contains(event.target as Node));
              const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
                : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
              items[next]?.focus();
            }}>
              {hasContent ? <button id="clearInputButton" type="button" role="menuitem" className="composer-view-menu-item" autoFocus
                onClick={() => { setMoreOpen(false); onClear(); }}>{t('composer.actions.clear')}</button> : null}
              {showInterrupt ? <button type="button" role="menuitem" className="composer-view-menu-item" autoFocus={!hasContent}
                title={t('composer.actions.changeDefault', DELIVERY_POLICY_LABELS[defaultDeliveryPolicy])}
                onClick={() => { setMoreOpen(false); onToggleDefault(); }}>
                {t('composer.actions.default', DELIVERY_POLICY_LABELS[defaultDeliveryPolicy])}
              </button> : null}
            </div>
          </AnchoredOverlay>
        </div>
      )}
      {showInterrupt && (
        <button
          id="interruptButton"
          type="button"
          className="composer-icon-button interrupt-button"
          onClick={onInterrupt}
          disabled={stopPending}
          aria-busy={stopPending}
          aria-label={t(stopPending ? "thread.status.requestingStop" : "composer.actions.interrupt")}
          title={t(stopPending ? "thread.status.requestingStop" : "composer.actions.interrupt")}
        >
          {stopPending ? <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" /> : <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <rect
              x="3"
              y="3"
              width="10"
              height="10"
              rx="2"
              fill="currentColor"
            />
          </svg>}
        </button>
      )}
      <button
        id="sendButton"
        data-testid="composer-send"
        type="button"
        className="send-button"
        disabled={!hasContent || sendBlocked}
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
          disabled={!hasContent || sendBlocked}
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
    </>
  );
}
