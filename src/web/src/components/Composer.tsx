import React, { useEffect, useMemo, useRef, useState } from "react";
import { useAppState } from "../hooks";
import type { ConversationContentView } from "../types/frontend";

type ViewOption = {
  id: ConversationContentView;
  label: string;
  description: string;
};

const VIEW_OPTIONS: ViewOption[] = [
  {
    id: "default",
    label: "默认视图",
    description: "显示对话消息、推理过程和 trace 细节",
  },
  {
    id: "agent",
    label: "Agent 视图",
    description: "查看 Agent State messages 快照",
  },
  // TODO: 后续添加更多视图时，在这里扩展菜单项并接入对应面板。
];

function resizeTextarea(textarea: HTMLTextAreaElement | null) {
  if (!textarea) {
    return;
  }

  textarea.style.height = "0px";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
}

function insertLineBreak(value: string, start: number, end: number): string {
  return value.slice(0, start) + "\n" + value.slice(end);
}

export default function Composer() {
  const { state, sendMessage, interruptSession, switchContentView } =
    useAppState();
  const [input, setInput] = useState("");
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const viewMenuRef = useRef<HTMLDivElement | null>(null);

  const hasContent = input.trim().length > 0;
  const currentAgent = state.currentSession?.current_agent_id || "default";
  const currentView =
    VIEW_OPTIONS.find((option) => option.id === state.contentView) ??
    VIEW_OPTIONS[0];
  const pendingConversations = state.currentSession
    ? (state.pendingConversations.get(state.currentSession.session_id) ?? [])
    : [];
  const showInterrupt = pendingConversations.some(
    (conversation) => conversation.pending,
  );
  const queuedCount = pendingConversations.filter(
    (conversation) => conversation.pending && conversation.status === "queued",
  ).length;
  const composerHint = useMemo(() => {
    if (showInterrupt) {
      return queuedCount > 0
        ? `正在生成，另有 ${queuedCount} 条消息排队`
        : "正在生成，可继续发送下一条或点击停止";
    }
    return "Enter 发送 · Ctrl+Enter 换行";
  }, [queuedCount, showInterrupt]);

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [input]);

  useEffect(() => {
    if (!viewMenuOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        viewMenuRef.current?.contains(target)
      ) {
        return;
      }
      setViewMenuOpen(false);
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [viewMenuOpen]);

  const handleSend = () => {
    const content = input.trim();
    if (!content) {
      return;
    }

    setInput("");
    void sendMessage(content).catch(() => {
      setInput(content);
    });
  };

  const handleInterrupt = () => {
    if (!showInterrupt) {
      return;
    }

    void interruptSession();
  };

  const handleViewSelect = (view: ConversationContentView) => {
    setViewMenuOpen(false);
    void switchContentView(view);
  };

  const handleViewMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") {
      return;
    }
    e.preventDefault();
    setViewMenuOpen(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter") {
      return;
    }

    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      const start = e.currentTarget.selectionStart ?? input.length;
      const end = e.currentTarget.selectionEnd ?? input.length;
      setInput(insertLineBreak(input, start, end));
      return;
    }

    if (e.shiftKey) {
      return;
    }

    e.preventDefault();
    handleSend();
  };

  return (
    <footer className="composer">
      <div className="composer-surface">
        <div className="composer-copy">
          <textarea
            ref={textareaRef}
            id="input"
            placeholder="输入消息后回车发送，Ctrl+Enter 换行"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
          />
        </div>
        <div className="composer-actions">
          <div className="composer-actions-left">
            <button
              id="attachButton"
              type="button"
              className="composer-icon-button"
              title="添加附件"
            >
              <svg
                viewBox="0 0 16 16"
                width="12"
                height="12"
                aria-hidden="true"
              >
                <path
                  d="M6.5 1.5a3.5 3.5 0 0 1 4.95 0l2.05 2.05a4.5 4.5 0 0 1-6.364 6.364l-3.18-3.18a2.5 2.5 0 0 1 3.535-3.535l2.121 2.121"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
            <div className="composer-hint">{composerHint}</div>
          </div>
          <div className="composer-actions-right">
            <div className="composer-actions-row">
              <div
                ref={viewMenuRef}
                className="composer-view-control"
                onKeyDown={handleViewMenuKeyDown}
              >
                <button
                  id="viewModeButton"
                  type="button"
                  className="composer-icon-button composer-view-button"
                  onClick={() => setViewMenuOpen((open) => !open)}
                  title={`打开视图菜单，当前：${currentView.label}。${currentView.description}`}
                  aria-label={`打开视图菜单，当前：${currentView.label}`}
                  aria-haspopup="menu"
                  aria-expanded={viewMenuOpen}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="12"
                    height="12"
                    aria-hidden="true"
                  >
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
                {viewMenuOpen && (
                  <div className="composer-view-menu" role="menu">
                    {VIEW_OPTIONS.map((option) => (
                      <button
                        key={option.id}
                        type="button"
                        className={`composer-view-menu-item${
                          option.id === state.contentView ? " active" : ""
                        }`}
                        role="menuitemradio"
                        aria-checked={option.id === state.contentView}
                        title={option.description}
                        onClick={() => handleViewSelect(option.id)}
                      >
                        <span className="composer-view-menu-label">
                          {option.label}
                        </span>
                        <span className="composer-view-menu-description">
                          {option.description}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                id="agentSelectButton"
                type="button"
                className="composer-agent-button"
                title={`选择Agent: ${currentAgent}`}
              >
                <span className="composer-agent-label">{currentAgent}</span>
                <svg viewBox="0 0 16 16" width="12" height="12">
                  <path d="M8 1L9 4H12L9.5 6L10.5 9L8 7L5.5 9L6.5 6L4 4H7L8 1ZM4 10L5 13H8L6.5 15L7.5 12H10.5L9.5 15H12.5L11.5 12H14.5L13 10H4z" />
                </svg>
              </button>
              {hasContent && (
                <button
                  id="clearInputButton"
                  type="button"
                  title="清空输入"
                  className="composer-icon-button hover-only"
                  onClick={() => setInput("")}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="12"
                    height="12"
                    aria-hidden="true"
                  >
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
                  onClick={handleInterrupt}
                  title="中断生成"
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="12"
                    height="12"
                    aria-hidden="true"
                  >
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
                type="button"
                className="send-button"
                disabled={!hasContent}
                onClick={handleSend}
                title={hasContent ? "发送消息" : "输入消息以启用发送"}
              >
                <svg viewBox="0 0 16 16" width="12" height="12">
                  <path d="M1.5 1.5L14.5 8L1.5 14.5V9L10 8L1.5 7V1.5Z" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </footer>
  );
}
