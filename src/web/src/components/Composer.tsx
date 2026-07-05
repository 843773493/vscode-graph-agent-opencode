import React, { useEffect, useMemo, useRef, useState } from "react";
import { useAppState } from "../hooks";
import { VIEW_OPTIONS } from "../state/contentViews";
import type { ConversationContentView } from "../types/frontend";
import {
  fileToSelectedAttachment,
  MEDIA_ONLY_PROMPT,
  mediaFilesFromClipboard,
  type SelectedAttachment,
} from "../utils/mediaAttachments";
import ComposerActionButtons from "./ComposerActionButtons";
import ComposerAgentControl from "./ComposerAgentControl";
import ComposerAttachmentTray from "./ComposerAttachmentTray";
import ComposerSlashCommandMenu, {
  type SlashCommandOption,
} from "./ComposerSlashCommandMenu";
import ComposerViewControl from "./ComposerViewControl";
import SessionNameDialog from "./SessionNameDialog";

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

function copyTextWithSelection(text: string): boolean {
  // TODO: 兼容本地浏览器禁用 Clipboard API 权限的场景；后续统一权限策略后可收敛。
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  const previousFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;

  let copied = false;
  try {
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    textarea.remove();
    previousFocus?.focus();
  }
  return copied;
}

function nextEnabledSlashCommandIndex(
  commands: SlashCommandOption[],
  currentIndex: number,
  direction: 1 | -1,
): number {
  if (commands.length === 0) {
    return 0;
  }
  for (let offset = 1; offset <= commands.length; offset += 1) {
    const nextIndex =
      (currentIndex + direction * offset + commands.length) % commands.length;
    if (!commands[nextIndex]?.disabled) {
      return nextIndex;
    }
  }
  return 0;
}

function firstEnabledSlashCommandIndex(commands: SlashCommandOption[]): number {
  const index = commands.findIndex((command) => !command.disabled);
  return index === -1 ? 0 : index;
}

function getSlashCommandArgs(inputValue: string, command: string): string {
  const trimmedInput = inputValue.trim();
  if (!trimmedInput.startsWith(command)) {
    return "";
  }

  const nextChar = trimmedInput.charAt(command.length);
  if (nextChar && !/\s/.test(nextChar)) {
    return "";
  }

  return trimmedInput.slice(command.length).trim();
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
    renameSession,
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
  const previousSessionIdRef = useRef<string | null>(currentSessionId);

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
  const slashCommands = useMemo<SlashCommandOption[]>(
    () => [
      {
        id: "quit",
        command: "/quit",
        title: "退出提示",
        description: "Show how to exit the web UI.",
      },
      {
        id: "new",
        command: "/new",
        title: "新建会话",
        description: "Start a new chat. 可输入名称。",
      },
      {
        id: "rename",
        command: "/rename",
        title: "命名会话",
        description: "Rename the current chat.",
      },
      {
        id: "init",
        command: "/init",
        title: "初始化 AGENTS.md",
        description: "暂未接入 Web 前端。",
        disabled: true,
      },
      {
        id: "clear",
        command: "/clear",
        title: "清空输入",
        description: "Clear the composer.",
      },
      {
        id: "copy",
        command: "/copy",
        title: "复制回复",
        description: "Copy the last assistant response.",
      },
      {
        id: "raw",
        command: "/raw",
        title: "事件视图",
        description: "Toggle raw trace event view.",
      },
      {
        id: "model",
        command: "/model",
        title: "打开 Agent 配置",
        description: "当前没有独立模型选择，模型由 Agent 配置决定。",
      },
      {
        id: "theme",
        command: "/theme",
        title: "切换主题",
        description: "暂未接入 Web 前端。",
        disabled: true,
      },
      {
        id: "default",
        command: "/default",
        title: "默认视图",
        description: "Show conversation timeline.",
      },
      {
        id: "events",
        command: "/events",
        title: "事件视图",
        description: "Show trace events.",
      },
      {
        id: "state",
        command: "/state",
        title: "Agent 视图",
        description: "Show Agent State messages.",
      },
      {
        id: "view",
        command: "/view",
        title: "选择视图",
        description: "Open the view picker.",
      },
      {
        id: "compact",
        command: "/compact",
        title: "压缩上下文",
        description: "Compact the current session.",
      },
      {
        id: "agent",
        command: "/agent",
        title: "选择 Agent",
        description: "Open the agent picker.",
      },
    ],
    [],
  );
  const slashQuery = useMemo(() => {
    if (!input.startsWith("/")) {
      return null;
    }
    const commandToken = input.slice(1).match(/^\S*/)?.[0] ?? "";
    return commandToken.toLowerCase();
  }, [input]);
  const matchingSlashCommands = useMemo(() => {
    if (slashQuery === null) {
      return [];
    }
    return slashCommands.filter((command) =>
      command.command.slice(1).startsWith(slashQuery),
    );
  }, [slashCommands, slashQuery]);
  const slashCommandMode = slashQuery !== null;

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [input]);

  useEffect(() => {
    if (previousSessionIdRef.current === currentSessionId) {
      return;
    }
    previousSessionIdRef.current = currentSessionId;
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
  }, [currentSessionId]);

  useEffect(() => {
    setSlashCommandIndex(firstEnabledSlashCommandIndex(matchingSlashCommands));
  }, [matchingSlashCommands]);

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

  const runSlashCommand = (command: SlashCommandOption, args = "") => {
    setInput("");
    setAttachmentError("");
    setComposerNotice("");
    switch (command.id) {
      case "quit":
        setStatus("Web 页面仍在运行，可关闭当前浏览器标签页");
        setAttachmentError("Web 端不退出本地服务，请直接关闭当前页面");
        break;
      case "new":
        setAttachments([]);
        void createSession(args.trim() || "新会话");
        break;
      case "rename":
        renameCurrentSession(args);
        break;
      case "init":
        setAttachmentError("/init 暂未接入 Web 前端");
        break;
      case "clear":
        setAttachments([]);
        setStatus("已清空输入");
        setComposerNotice("已清空输入和未发送附件");
        break;
      case "copy": {
        const latestAssistantMessage = [...state.messages]
          .reverse()
          .find(
            (message) =>
              message.role === "assistant" && message.content.trim().length > 0,
          );
        if (!latestAssistantMessage) {
          setAttachmentError("没有可复制的助手回复");
          break;
        }
        if (copyTextWithSelection(latestAssistantMessage.content)) {
          setStatus("已复制最近助手回复");
          setComposerNotice("已复制最近助手回复");
          break;
        }
        if (!navigator.clipboard) {
          setAttachmentError("当前浏览器不支持剪贴板写入");
          break;
        }
        void navigator.clipboard
          .writeText(latestAssistantMessage.content)
          .then(() => {
            setStatus("已复制最近助手回复");
            setComposerNotice("已复制最近助手回复");
          })
          .catch((error: unknown) => {
            setAttachmentError(
              `复制失败：${error instanceof Error ? error.message : String(error)}`,
            );
          });
        break;
      }
      case "raw":
        void switchContentView("events");
        break;
      case "model":
        setAgentMenuOpen(true);
        setStatus("已打开 Agent 配置选择");
        setComposerNotice("当前没有独立模型选择，模型由 Agent 配置决定");
        break;
      case "agent":
        setAgentMenuOpen(true);
        break;
      case "theme":
        setAttachmentError("/theme 暂未接入 Web 前端");
        break;
      case "view":
        setViewMenuOpen(true);
        break;
      case "default":
        void switchContentView("default");
        break;
      case "events":
        void switchContentView("events");
        break;
      case "state":
        void switchContentView("agent");
        break;
      case "compact":
        if (state.currentSession && !state.compactLoading) {
          void compactSession();
        }
        break;
      default:
        break;
    }
  };

  const submitSlashInput = () => {
    if (!slashCommandMode) {
      return false;
    }

    const command =
      matchingSlashCommands[slashCommandIndex] ??
      matchingSlashCommands[0];
    const commandArgs = command
      ? getSlashCommandArgs(input, command.command)
      : "";
    setInput("");
    setComposerNotice("");
    if (command && !command.disabled) {
      runSlashCommand(command, commandArgs);
    } else if (command?.disabled) {
      setAttachmentError(`${command.command} 暂未接入 Web 前端`);
    } else {
      setAttachmentError(`未知指令：/${slashQuery}`);
    }
    return true;
  };

  const handleSend = () => {
    if (submitSlashInput()) {
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
    ).catch(() => {
      setInput(content);
      setAttachments(sentAttachments);
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
        submitSlashInput();
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
      <footer className="composer">
      <div className="composer-surface">
        <div className="composer-copy">
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
            placeholder="输入消息后回车发送，输入 / 查看指令"
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
        <div className="composer-actions">
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
              className="composer-icon-button"
              onClick={handleAttachClick}
              title="添加附件"
              aria-label="添加图片或视频附件"
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
