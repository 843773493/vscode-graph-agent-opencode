import React from "react";
import type { SlashCommandOption } from "../state/slashCommands";

export default function ComposerSlashCommandMenu({
  query,
  commands,
  activeIndex,
  onSelect,
}: {
  query: string | null;
  commands: SlashCommandOption[];
  activeIndex: number;
  onSelect: (command: SlashCommandOption) => void;
}): React.ReactNode {
  if (query === null) {
    return null;
  }

  return (
    <div
      className="composer-slash-menu"
      role="listbox"
      aria-label="斜杠指令"
    >
      {commands.length > 0 ? (
        <>
          {commands.map((command, index) => (
            <button
              key={command.id}
              type="button"
              className={`composer-slash-menu-item${index === activeIndex && !command.disabled ? " active" : ""}${command.disabled ? " disabled" : ""}`}
              role="option"
              aria-selected={index === activeIndex}
              disabled={command.disabled}
              onMouseDown={(event) => {
                event.preventDefault();
                if (command.disabled) {
                  return;
                }
                onSelect(command);
              }}
            >
              <span className="composer-slash-command">{command.command}</span>
              <span className="composer-slash-copy">
                <span className="composer-slash-title">{command.title}</span>
                <span className="composer-slash-description">
                  {command.description}
                </span>
              </span>
            </button>
          ))}
          {commands.length > 8 ? (
            <div className="composer-slash-more" role="presentation">
              滚动查看更多指令
            </div>
          ) : null}
        </>
      ) : (
        <div
          className="composer-slash-empty"
          role="option"
          aria-selected={false}
          aria-disabled="true"
        >
          <span className="composer-slash-command">/{query}</span>
          <span className="composer-slash-copy">
            <span className="composer-slash-title">未找到指令</span>
            <span className="composer-slash-description">
              输入 / 查看可用指令
            </span>
          </span>
        </div>
      )}
    </div>
  );
}
