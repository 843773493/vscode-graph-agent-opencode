import React, { useEffect, useMemo, useRef, useState } from "react";
import { useAppState } from "../hooks";
import { VIEW_OPTIONS } from "../state/contentViews";
import type { ConversationContentView } from "../types/frontend";
import {
  fileToSelectedAttachment,
  IMAGE_ONLY_PROMPT,
  imageFilesFromClipboard,
  type SelectedAttachment,
} from "../utils/imageAttachments";
import ComposerActionButtons from "./ComposerActionButtons";
import ComposerAgentControl from "./ComposerAgentControl";
import ComposerAttachmentTray from "./ComposerAttachmentTray";
import ComposerViewControl from "./ComposerViewControl";

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
  const {
    state,
    sendMessage,
    compactSession,
    interruptSession,
    switchAgent,
    switchContentView,
  } =
    useAppState();
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<SelectedAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const viewMenuRef = useRef<HTMLDivElement | null>(null);
  const agentMenuRef = useRef<HTMLDivElement | null>(null);

  const hasContent = input.trim().length > 0 || attachments.length > 0;
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

  useEffect(() => {
    if (!agentMenuOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        agentMenuRef.current?.contains(target)
      ) {
        return;
      }
      setAgentMenuOpen(false);
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [agentMenuOpen]);

  const handleSend = () => {
    const typedContent = input.trim();
    if (!typedContent && attachments.length === 0) {
      return;
    }

    const content = typedContent || IMAGE_ONLY_PROMPT;
    const sentAttachments = attachments;
    setInput("");
    setAttachments([]);
    setAttachmentError("");
    void sendMessage(
      content,
      sentAttachments.map(({ previewUrl, ...attachment }) => attachment),
    ).catch(() => {
      setInput(content);
      setAttachments(sentAttachments);
    });
  };

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }

    try {
      const attachment = await fileToSelectedAttachment(file);
      setAttachments([attachment]);
      setAttachmentError("");
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : String(error));
    }
  };

  const handlePaste = async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const imageFiles = imageFilesFromClipboard(event.clipboardData);
    if (imageFiles.length === 0) {
      return;
    }

    event.preventDefault();
    try {
      const pastedAttachments = await Promise.all(
        imageFiles.map((file, index) => fileToSelectedAttachment(file, index)),
      );
      setAttachments(pastedAttachments);
      setAttachmentError("");
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : String(error));
    }
  };

  const handleRemoveAttachment = () => {
    setAttachments([]);
    setAttachmentError("");
  };

  const handleClear = () => {
    setInput("");
    setAttachments([]);
    setAttachmentError("");
  };

  const handleInterrupt = () => {
    if (!showInterrupt) {
      return;
    }

    void interruptSession();
  };

  const handleCompact = () => {
    if (!state.currentSession || state.compactLoading) {
      return;
    }

    void compactSession();
  };

  const handleViewSelect = (view: ConversationContentView) => {
    setViewMenuOpen(false);
    void switchContentView(view);
  };

  const handleAgentSelect = (agentId: string) => {
    setAgentMenuOpen(false);
    void switchAgent(agentId).catch(() => {
      // 错误状态由 AppProvider 写入，菜单这里不吞掉后端错误表现。
    });
  };

  const handleViewMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") {
      return;
    }
    e.preventDefault();
    setViewMenuOpen(false);
  };

  const handleAgentMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") {
      return;
    }
    e.preventDefault();
    setAgentMenuOpen(false);
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
            onPaste={handlePaste}
            rows={1}
          />
          <ComposerAttachmentTray
            attachments={attachments}
            error={attachmentError}
            onRemove={handleRemoveAttachment}
          />
        </div>
        <div className="composer-actions">
          <div className="composer-actions-left">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              className="composer-file-input"
              onChange={handleFileChange}
            />
            <button
              id="attachButton"
              type="button"
              className="composer-icon-button"
              onClick={handleAttachClick}
              title="添加附件"
              aria-label="添加图片附件"
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
              <ComposerViewControl
                controlRef={viewMenuRef}
                currentView={currentView}
                selectedView={state.contentView}
                open={viewMenuOpen}
                onToggle={() => setViewMenuOpen((open) => !open)}
                onSelect={handleViewSelect}
                onKeyDown={handleViewMenuKeyDown}
              />
              <ComposerAgentControl
                controlRef={agentMenuRef}
                agents={state.agents}
                currentAgent={currentAgent}
                open={agentMenuOpen}
                onToggle={() => setAgentMenuOpen((open) => !open)}
                onSelect={handleAgentSelect}
                onKeyDown={handleAgentMenuKeyDown}
              />
              <ComposerActionButtons
                hasContent={hasContent}
                hasSession={Boolean(state.currentSession)}
                compactLoading={state.compactLoading}
                showInterrupt={showInterrupt}
                onCompact={handleCompact}
                onClear={handleClear}
                onInterrupt={handleInterrupt}
                onSend={handleSend}
              />
            </div>
          </div>
        </div>
      </div>
    </footer>
  );
}
