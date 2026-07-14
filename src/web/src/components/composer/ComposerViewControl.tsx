import React from "react";
import {
  COMPOSER_VIEW_OPTIONS,
  type ViewOption,
} from "../../state/contentViews";
import type { ConversationContentView } from "../../types/frontend";

export default function ComposerViewControl({
  controlRef,
  currentView,
  selectedView,
  open,
  onToggle,
  onSelect,
  onKeyDown,
}: {
  controlRef: React.RefObject<HTMLDivElement>;
  currentView: ViewOption;
  selectedView: ConversationContentView;
  open: boolean;
  onToggle: () => void;
  onSelect: (view: ConversationContentView) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
}): React.ReactNode {
  return (
    <div
      ref={controlRef}
      className="composer-view-control"
      onKeyDown={onKeyDown}
    >
      <button
        id="viewModeButton"
        type="button"
        className="composer-icon-button composer-view-button"
        onClick={onToggle}
        title={`打开视图菜单，当前：${currentView.label}。${currentView.description}`}
        aria-label={`打开视图菜单，当前：${currentView.label}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
          <path
            d="M2.5 4.5A1.5 1.5 0 0 1 4 3h8a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 12 13H4a1.5 1.5 0 0 1-1.5-1.5v-7Zm3 0v7M7.5 6h4M7.5 8.5h4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open && (
        <div className="composer-view-menu" role="menu">
          {COMPOSER_VIEW_OPTIONS.map((option) => (
            <button
              key={option.id}
              type="button"
              className={`composer-view-menu-item${
                option.id === selectedView ? " active" : ""
              }`}
              role="menuitemradio"
              aria-checked={option.id === selectedView}
              title={option.description}
              onClick={() => onSelect(option.id)}
            >
              <span className="composer-view-menu-label">{option.label}</span>
              <span className="composer-view-menu-description">
                {option.description}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
