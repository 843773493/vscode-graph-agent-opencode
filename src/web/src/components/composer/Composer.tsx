import React, { useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import { useAppState } from "../../hooks";
import { useComposerSlashCommands } from "../../hooks/useComposerSlashCommands";
import { VIEW_OPTIONS } from "../../state/contentViews";
import { sessionScopeKey } from "../../state/sessionScope";
import {
  firstEnabledSlashCommandIndex,
  getSlashCommandArgs,
  nextEnabledSlashCommandIndex,
} from "../../state/slashCommands";
import type { ConversationContentView } from "../../types/frontend";
import {
  fileToSelectedAttachment,
  MEDIA_ONLY_PROMPT,
  mediaFilesFromClipboard,
  type SelectedAttachment,
} from "../../utils/mediaAttachments";
import ComposerActionButtons from "./ComposerActionButtons";
import ComposerAgentControl from "./ComposerAgentControl";
import ComposerAttachmentTray from "./ComposerAttachmentTray";
import ComposerSlashCommandMenu from "./ComposerSlashCommandMenu";
import ComposerToolControl from "./ComposerToolControl";
import ComposerViewControl from "./ComposerViewControl";
import SessionNameDialog from "../SessionNameDialog";
import WorkspaceSwitcher from "../workspace/WorkspaceSwitcher";

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
    setStatus,
    sendMessage,
    compactSession,
    interruptSession,
    switchAgent,
    switchContentView,
    createSession,
    startNewSessionDraft,
    renameSession,
    activateGatewayWorkspace,
    addLocalGatewayWorkspace,
    addSshGatewayWorkspace,
  } =
    useAppState();
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<SelectedAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [composerNotice, setComposerNotice] = useState("");
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const [slashCommandIndex, setSlashCommandIndex] = useState(0);
  const [renameDialogOpen, setRenameDialogOpen] = useState(false);
  const [renameDialogSubmitting, setRenameDialogSubmitting] = useState(false);
  const [renameDialogError, setRenameDialogError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const viewMenuRef = useRef<HTMLDivElement | null>(null);
  const agentMenuRef = useRef<HTMLDivElement | null>(null);
  const currentSessionId = state.currentSession?.session_id ?? null;
  const currentWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  const currentSessionCacheKey =
    currentSessionId && currentWorkspaceId
      ? sessionScopeKey(currentWorkspaceId, currentSessionId)
      : currentSessionId;
  const previousSessionIdRef = useRef<string | null>(currentSessionCacheKey);

  const hasContent = input.trim().length > 0 || attachments.length > 0;
  const currentAgent = state.currentSession?.current_agent_id || "default";
  const currentAgentConfig = state.agents.find(
    (agent) => agent.agent_id === currentAgent,
  );
  const currentModel = currentAgentConfig?.model || "model";
  const currentView =
    VIEW_OPTIONS.find((option) => option.id === state.contentView) ??
    VIEW_OPTIONS[0];
  const pendingConversations = state.currentSession
    ? (state.pendingConversations.get(currentSessionCacheKey ?? state.currentSession.session_id) ?? [])
    : [];
  const hasCurrentSessionHistory =
    Boolean(state.currentSession) &&
    (state.messages.length > 0 ||
      pendingConversations.length > 0 ||
      state.currentSession?.title_source !== "default");
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
    if (previousSessionIdRef.current === currentSessionCacheKey) {
      return;
    }
    previousSessionIdRef.current = currentSessionCacheKey;
    setInput("");
    setAttachments([]);
    setAttachmentError("");
    setComposerNotice("");
    setAgentMenuOpen(false);
    setViewMenuOpen(false);
    setSlashCommandIndex(0);
    setRenameDialogOpen(false);
    setRenameDialogError(null);
    setRenameDialogSubmitting(false);
  }, [currentSessionCacheKey]);

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

  const renameCurrentSession = (inlineTitle: string) => {
    const session = state.currentSession;
    if (!session) {
      setAttachmentError("当前没有可命名的会话");
      return;
    }

    const title = inlineTitle.trim();
    if (!title) {
      setRenameDialogError(null);
      setRenameDialogOpen(true);
      return;
    }

    void renameSession(session.session_id, title)
      .then(() => {
        setComposerNotice(`已命名为 ${title}`);
      })
      .catch((error: unknown) => {
        setAttachmentError(
          `命名失败：${error instanceof Error ? error.message : String(error)}`,
        );
      });
  };

  const submitRenameDialog = (title: string) => {
    const session = state.currentSession;
    if (!session) {
      setRenameDialogError("当前没有可命名的会话");
      return;
    }

    setRenameDialogSubmitting(true);
    setRenameDialogError(null);
    void renameSession(session.session_id, title)
      .then(() => {
        setRenameDialogOpen(false);
        setComposerNotice(`已命名为 ${title}`);
      })
      .catch((error: unknown) => {
        setRenameDialogError(
          error instanceof Error ? error.message : String(error),
        );
      })
      .finally(() => {
        setRenameDialogSubmitting(false);
      });
  };

  const closeRenameDialog = () => {
    if (renameDialogSubmitting) {
      return;
    }
    setRenameDialogOpen(false);
    setRenameDialogError(null);
  };

  const {
    slashQuery,
    matchingSlashCommands,
    slashCommandMode,
    runSlashCommand,
    submitSlashInput,
  } = useComposerSlashCommands({
    input,
    state,
    setInput,
    setAttachments,
    setAttachmentError,
    setComposerNotice,
    setAgentMenuOpen,
    setViewMenuOpen,
    setStatus,
    createSession,
    startNewSessionDraft,
    renameCurrentSession,
    switchContentView,
    compactSession,
  });

  useEffect(() => {
    setSlashCommandIndex(firstEnabledSlashCommandIndex(matchingSlashCommands));
  }, [matchingSlashCommands]);

  const handleSend = () => {
    if (submitSlashInput(slashCommandIndex)) {
      return;
    }

    const typedContent = input.trim();
    if (!typedContent && attachments.length === 0) {
      return;
    }

    const content = typedContent || MEDIA_ONLY_PROMPT;
    const sentAttachments = attachments;
    setInput("");
    setAttachments([]);
    setAttachmentError("");
    setComposerNotice("");
    void sendMessage(
      content,
      sentAttachments.map((attachment) => ({
        file_id: attachment.file_id,
        name: attachment.name,
        content_type: attachment.content_type,
        data_url: attachment.data_url,
      })),
    ).catch((error: unknown) => {
      setInput(content);
      setAttachments(sentAttachments);
      setAttachmentError(
        `发送失败：${error instanceof Error ? error.message : String(error)}`,
      );
    });
  };

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const appendFilesToAttachments = async (files: File[]) => {
    if (files.length === 0) {
      return;
    }

    const results = await Promise.allSettled(
      files.map((file, index) =>
        fileToSelectedAttachment(file, attachments.length + index),
      ),
    );
    const nextAttachments = results.flatMap((result) =>
      result.status === "fulfilled" ? [result.value] : [],
    );
    const errors = results.flatMap((result) =>
      result.status === "rejected"
        ? [result.reason instanceof Error ? result.reason.message : String(result.reason)]
        : [],
    );

    if (nextAttachments.length > 0) {
      setAttachments((current) => [...current, ...nextAttachments]);
    }
    if (errors.length > 0) {
      setAttachmentError(errors.join("；"));
    } else {
      setAttachmentError("");
    }
    setComposerNotice("");
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    void appendFilesToAttachments(files);
  };

  const handlePaste = async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const mediaFiles = mediaFilesFromClipboard(event.clipboardData);
    if (mediaFiles.length === 0) {
      return;
    }

    event.preventDefault();
    await appendFilesToAttachments(mediaFiles);
  };

  const handleRemoveAttachment = (fileId: string) => {
    setAttachments((current) =>
      current.filter((attachment) => attachment.file_id !== fileId),
    );
    setAttachmentError("");
    setComposerNotice("");
  };

  const handleClear = () => {
    setInput("");
    setAttachments([]);
    setAttachmentError("");
    setComposerNotice("");
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
    if (slashCommandMode) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashCommandIndex((index) =>
          nextEnabledSlashCommandIndex(matchingSlashCommands, index, 1),
        );
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashCommandIndex((index) =>
          nextEnabledSlashCommandIndex(matchingSlashCommands, index, -1),
        );
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault();
        submitSlashInput(slashCommandIndex);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setInput("");
        setAttachmentError("");
        setComposerNotice("");
        return;
      }
    }

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
    <>
      <footer className="composer new-chat-widget-content">
        {!hasCurrentSessionHistory ? (
          <div className="new-session-workspace-picker-container">
            <div className="session-workspace-picker">
              <span className="session-workspace-picker-label">
                新会话位于
              </span>
              <WorkspaceSwitcher
                apiPort={state.apiPort ?? DEFAULT_BACKEND_PORT}
                workspaces={state.gatewayWorkspaces}
                activeWorkspaceId={state.activeGatewayWorkspaceId}
                recentLocalWorkspacePaths={state.uiSettings.recent_local_workspace_paths}
                switching={state.workspaceSwitching}
                onActivate={activateGatewayWorkspace}
                onAddLocal={addLocalGatewayWorkspace}
                onAddSsh={addSshGatewayWorkspace}
              />
              <span className="session-workspace-picker-label session-workspace-picker-with-label">
                使用
              </span>
              <span
                className="sessions-chat-picker-slot sessions-chat-session-type-picker fixed"
                title="本地运行时"
                aria-label="使用本地运行时"
              >
                <span className="picker-icon" aria-hidden="true">▱</span>
                <span className="sessions-chat-dropdown-label">本地</span>
              </span>
            </div>
          </div>
        ) : null}
        <div className="composer-surface new-chat-input-container">
          <div className="new-chat-input-area">
            <div className="composer-copy sessions-chat-editor">
              <ComposerSlashCommandMenu
                query={slashQuery}
                commands={matchingSlashCommands}
                activeIndex={slashCommandIndex}
                onSelect={(command) =>
                  runSlashCommand(command, getSlashCommandArgs(input, command.command))
                }
              />
              <textarea
                ref={textareaRef}
                id="input"
                placeholder="你的路线图下一步是什么？"
                value={input}
                onChange={(e) => {
                  setInput(e.target.value);
                  setComposerNotice("");
                  setAttachmentError("");
                }}
                onKeyDown={handleKeyDown}
                onPaste={handlePaste}
                rows={1}
              />
              <ComposerAttachmentTray
                attachments={attachments}
                error={attachmentError}
                notice={composerNotice}
                onRemove={handleRemoveAttachment}
              />
            </div>
            <div className="composer-actions sessions-chat-toolbar">
              <div className="composer-actions-left">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept="image/*,video/mp4,video/webm,video/quicktime,video/x-matroska"
                  className="composer-file-input"
                  onChange={handleFileChange}
                />
                <button
                  id="attachButton"
                  type="button"
                  className="composer-icon-button sessions-chat-attach-button"
                  onClick={handleAttachClick}
                  title="添加附件"
                  aria-label="添加图片或视频附件"
                >
                  +
                </button>
                <ComposerViewControl
                  controlRef={viewMenuRef}
                  currentView={currentView}
                  selectedView={state.contentView}
                  open={viewMenuOpen}
                  onToggle={() => setViewMenuOpen((open) => !open)}
                  onSelect={handleViewSelect}
                  onKeyDown={handleViewMenuKeyDown}
                />
                <div className="composer-hint">{composerHint}</div>
              </div>
              <div className="composer-actions-right">
                <div className="composer-actions-row sessions-chat-config-toolbar">
                  <ComposerAgentControl
                    controlRef={agentMenuRef}
                    agents={state.agents}
                    currentAgent={currentAgent}
                    open={agentMenuOpen}
                    onToggle={() => setAgentMenuOpen((open) => !open)}
                    onSelect={handleAgentSelect}
                    onKeyDown={handleAgentMenuKeyDown}
                  />
                  <button
                    type="button"
                    className="composer-model-pill"
                    title={`当前模型：${currentModel}`}
                    onClick={() => setStatus("模型由当前 Agent 配置决定，可通过 Agent 菜单切换")}
                  >
                    <span
                      className="codicon codicon-chip composer-picker-button-icon"
                      aria-hidden="true"
                    />
                    <span className="composer-model-label">{currentModel}</span>
                  </button>
                  <ComposerToolControl
                    apiPort={state.apiPort ?? DEFAULT_BACKEND_PORT}
                    agentId={currentAgent}
                    workspaceId={currentWorkspaceId}
                    onStatus={setStatus}
                  />
                  <ComposerActionButtons
                    hasContent={hasContent}
                    showInterrupt={showInterrupt}
                    onClear={handleClear}
                    onInterrupt={handleInterrupt}
                    onSend={handleSend}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </footer>
      <SessionNameDialog
        open={renameDialogOpen}
        title="命名当前会话"
        label="会话名称"
        initialValue={state.currentSession?.title || "新会话"}
        confirmText="保存名称"
        submitting={renameDialogSubmitting}
        error={renameDialogError}
        onCancel={closeRenameDialog}
        onSubmit={submitRenameDialog}
      />
    </>
  );
}
