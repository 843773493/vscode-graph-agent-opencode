import { t } from "../../../../shared/core/i18n/index.js";
import React, { useRef, useState } from "react";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import type { DeliveryPolicy } from "../../types/backend";
import { DELIVERY_POLICY_LABELS } from "../deliveryPolicyPresentation";
import type { ExecutionControlAction } from "../../api/jobs";

type StopAction = Exclude<ExecutionControlAction, "resume">;
const STOP_ACTIONS = ["pause", "interrupt", "stop"] as const;

export default function ComposerActionButtons({
  hasContent,
  sendBlocked = false,
  showInterrupt,
  stopPending = false,
  pausing = false,
  onClear,
  onControl,
  onRefreshControl,
  canResume = false,
  controlReady = false,
  advancementPaused = false,
  onSend,
  onAlternate,
  onToggleDefault,
  defaultDeliveryPolicy,
}: {
  hasContent: boolean;
  sendBlocked?: boolean;
  showInterrupt: boolean;
  stopPending?: boolean;
  pausing?: boolean;
  onClear: () => void;
  onControl: (action: ExecutionControlAction) => void;
  onRefreshControl: () => void;
  canResume?: boolean;
  controlReady?: boolean;
  advancementPaused?: boolean;
  onSend: () => void;
  onAlternate: () => void;
  onToggleDefault: () => void;
  defaultDeliveryPolicy: DeliveryPolicy;
}): React.ReactNode {
  const moreRef = useRef<HTMLDivElement>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const actionRef = useRef<HTMLDivElement>(null);
  const [controlMenu, setControlMenu] = useState<{ x: number; y: number } | null>(null);
  const [defaultStopAction, setDefaultStopAction] = useState<StopAction>("pause");
  const openControlMenu = (event: React.MouseEvent) => {
    event.preventDefault();
    setControlMenu({ x: event.clientX, y: event.clientY });
    onRefreshControl();
  };
  const controlDisabled = !controlReady || stopPending;
  const primaryEnabled = hasContent ? !sendBlocked : canResume && !controlDisabled;
  const primaryLabel = hasContent ? t("composer.actions.send") : canResume
    ? t("composer.execution.resume") : t("composer.actions.emptySend");
  const stopLabel = stopPending ? t("thread.status.requestingStop") : pausing
    ? `${t("composer.execution.pause")}…` : t(`composer.execution.${defaultStopAction}`);
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
      <div ref={actionRef} className="composer-more-control" onContextMenu={openControlMenu}
        onKeyDown={event => {
          if (event.key === "ContextMenu" || event.shiftKey && event.key === "F10") {
            event.preventDefault();
            const bounds = actionRef.current?.getBoundingClientRect();
            setControlMenu({ x: bounds?.right ?? 0, y: bounds?.top ?? 0 });
            onRefreshControl();
          }
        }}>
      {showInterrupt && (
        <button
          id="interruptButton"
          type="button"
          className="composer-icon-button interrupt-button"
          onClick={() => onControl(defaultStopAction)}
          disabled={controlDisabled || advancementPaused && defaultStopAction === "pause"}
          aria-busy={stopPending || pausing}
          aria-label={stopLabel}
          title={stopLabel}
        >
          {stopPending || pausing ? <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" /> : <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
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
        disabled={hasContent && sendBlocked}
        aria-disabled={!primaryEnabled}
        onClick={() => { if (primaryEnabled) { if (hasContent) onSend(); else onControl("resume"); } }}
        title={primaryLabel}
        aria-label={primaryLabel}
        aria-haspopup="menu"
        aria-expanded={controlMenu !== null}
      >
        {!hasContent && canResume ? <span className="codicon codicon-play" aria-hidden="true" /> : <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" />
        </svg>}
      </button>
      <AnchoredOverlay open={controlMenu !== null} anchorRef={actionRef} point={controlMenu ?? undefined}
        placement="top-end" manageFocus onClose={() => setControlMenu(null)}>
        <div className="composer-view-menu" role="menu" aria-label={t("composer.execution.menu")} onKeyDown={event => {
          if (event.key === "Escape") { event.preventDefault(); setControlMenu(null); actionRef.current?.querySelector("button")?.focus(); return; }
          if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
          const index = items.indexOf(event.target as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
            : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
          items[next]?.focus();
        }}>
          {canResume && <button type="button" role="menuitem" className="composer-view-menu-item"
            disabled={controlDisabled} onClick={() => { setControlMenu(null); onControl("resume"); }}>
            {t("composer.execution.resume")}
          </button>}
          {STOP_ACTIONS.map(action => <button key={action} type="button" role="menuitem" className="composer-view-menu-item"
            disabled={controlDisabled || action === "pause" && advancementPaused || action === "interrupt" && !showInterrupt}
            onClick={() => { setControlMenu(null); onControl(action); }}>
            {t(`composer.execution.${action}`)}
          </button>)}
          <div role="separator" />
          <span className="composer-hint">{t("composer.execution.default")}</span>
          {STOP_ACTIONS.map(action => <button key={action} type="button" role="menuitemradio"
            aria-checked={defaultStopAction === action} className="composer-view-menu-item"
            onClick={() => { setDefaultStopAction(action); setControlMenu(null); }}>
            {t(`composer.execution.${action}`)}{action === "pause" ? ` ${t("composer.execution.recommended")}` : ""}
          </button>)}
        </div>
      </AnchoredOverlay>
      </div>
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
