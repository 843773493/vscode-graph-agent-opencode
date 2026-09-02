import React, { useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import { useComposerState } from "../../hooks";
import { useComposerSlashCommands } from "../../hooks/useComposerSlashCommands";
import { useComposerDraft } from "../../hooks/useComposerDraft";
import { VIEW_OPTIONS } from "../../state/contentViews";
import { sessionScopeKey } from "../../state/session/sessionScope";
import {
  firstEnabledSlashCommandIndex,
  getSlashCommandArgs,
  nextEnabledSlashCommandIndex,
} from "../../state/slashCommands";
import type { ConversationContentView } from "../../types/frontend";
import type { DeliveryPolicy } from "../../types/backend";
import {
  fileToSelectedAttachment,
  MEDIA_ONLY_PROMPT,
  mediaFilesFromClipboard,
  type SelectedAttachment,
} from "../../utils/mediaAttachments";
import ComposerActionButtons from "./ComposerActionButtons";
import ComposerAgentControl from "./ComposerAgentControl";
import ComposerModelControl from "./ComposerModelControl";
import ComposerAttachmentTray from "./ComposerAttachmentTray";
import ComposerSlashCommandMenu from "./ComposerSlashCommandMenu";
import ComposerToolControl from "./ComposerToolControl";
import ComposerViewControl from "./ComposerViewControl";
import SessionNameDialog from "../SessionNameDialog";
import WarmActionDialog from "../WarmActionDialog";
import { useWarmConfirm } from "../WarmConfirmProvider";
import {
  formatBrowserElementSelections,
  parseBrowserElementSelectionBundle,
  type BrowserElementSelection,
} from "../../utils/browserElementSelection";
import {
  GOAL_STATUS_LABELS,
  goalCanResume,
  goalEditStatus,
  goalNeedsReplacementConfirmation,
  parseGoalSlashAction,
} from "../../state/sessionGoal";

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

function nextDeliveryPolicy(policy: DeliveryPolicy): DeliveryPolicy {
  if (policy === "after_turn") {
    return "after_tool_result";
  }
  if (policy === "after_tool_result") {
    return "after_interrupt";
  }
  return "after_turn";
}

function Composer() {
  const {
    state,
    setStatus,
    sendMessage,
    compactSession,
    refreshGoal,
    updateGoal,
    clearGoal,
    interruptSession,
    switchAgent,
    switchModel,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
    switchContentView,
    createSession,
    renameSession,
    updateUiSettings,
    getLatestAssistantContent,
  } =
    useComposerState();
  const [attachments, setAttachments] = useState<SelectedAttachment[]>([]);
  const [browserElements, setBrowserElements] = useState<BrowserElementSelection[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [composerNotice, setComposerNotice] = useState("");
  const [viewMenuOpen, setViewMenuOpen] = useState(false);
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [slashCommandIndex, setSlashCommandIndex] = useState(0);
  const [renameDialogOpen, setRenameDialogOpen] = useState(false);
  const [renameDialogSubmitting, setRenameDialogSubmitting] = useState(false);
  const [renameDialogError, setRenameDialogError] = useState<string | null>(null);
  const [goalEditOpen, setGoalEditOpen] = useState(false);
  const confirm = useWarmConfirm();
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const viewMenuRef = useRef<HTMLDivElement | null>(null);
  const agentMenuRef = useRef<HTMLDivElement | null>(null);
  const modelMenuRef = useRef<HTMLDivElement | null>(null);
  const currentSessionId = state.currentSession?.session_id ?? null;
  const currentWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  const currentSessionCacheKey =
    currentSessionId && currentWorkspaceId
      ? sessionScopeKey(currentWorkspaceId, currentSessionId)
      : currentSessionId;
  const [input, setInput] = useComposerDraft(
    currentWorkspaceId,
    currentSessionId,
    state.gatewayUserScope,
  );
  // TODO: 附件包含 data URL，后续使用 IndexedDB 恢复；本轮只持久化文本草稿。
  const previousSessionIdRef = useRef<string | null>(currentSessionCacheKey);
  const visibleGoal = state.currentGoalSessionId === currentSessionId
    ? state.currentGoal
    : null;

  const hasContent = input.trim().length > 0 || attachments.length > 0 || browserElements.length > 0;
  const currentAgent =
    state.currentSession?.current_agent_id
    ?? state.agents.find((agent) => agent.workspace_default)?.agent_id
    ?? "default";
  const currentAgentConfig = state.agents.find(
    (agent) => agent.agent_id === currentAgent,
  );
  const currentProviders = currentAgentConfig?.providers ?? [];
  const currentProviderId =
    state.currentSession?.current_provider_id
    ?? currentProviders.find((provider) => provider.workspace_default)?.provider_id
    ?? currentProviders[0]?.provider_id
    ?? currentAgentConfig?.model
    ?? "model";
  const defaultDeliveryPolicy =
    state.uiSettings.layout.delivery_policy_default ?? "after_turn";
  const currentView =
    VIEW_OPTIONS.find((option) => option.id === state.contentView) ??
    VIEW_OPTIONS[0];
  const showInterrupt = Boolean(state.currentActiveJobId);
  const queuedCount = state.queuedPendingCount;
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
    setAttachments([]);
    setBrowserElements([]);
    setAttachmentError("");
    setComposerNotice("");
    setAgentMenuOpen(false);
    setModelMenuOpen(false);
    setViewMenuOpen(false);
    setSlashCommandIndex(0);
    setRenameDialogOpen(false);
    setRenameDialogError(null);
    setRenameDialogSubmitting(false);
    setGoalEditOpen(false);
  }, [currentSessionCacheKey]);

  useEffect(() => {
    const acceptBrowserElementSelection = (value: unknown) => {
      const bundle = parseBrowserElementSelectionBundle(value);
      if (!bundle || bundle.workspaceId !== currentWorkspaceId) {
        return;
      }
      setBrowserElements((current) => {
        const base = current.filter((item) => item.browserId !== bundle.browserId);
        return bundle.elements.reduce((next, selection) => [
          ...next.filter((item) =>
            item.browserId !== selection.browserId || item.ref !== selection.ref
          ),
          selection,
        ], base);
      });
      const lastSelection = bundle.elements[bundle.elements.length - 1];
      setComposerNotice(
        bundle.mode === "rich"
          ? `已选择页面元素 <${lastSelection.tag}>（完整元素上下文）`
          : `已选择页面元素 <${lastSelection.tag}>`,
      );
      textareaRef.current?.focus();
    };
    const handleBrowserElementSelected = (event: MessageEvent<unknown>) => {
      if (event.origin === window.location.origin) {
        acceptBrowserElementSelection(event.data);
      }
    };
    const channel = new BroadcastChannel("boxteam-browser-elements");
    channel.addEventListener("message", (event) => acceptBrowserElementSelection(event.data));
    window.addEventListener("message", handleBrowserElementSelected);
    return () => {
      window.removeEventListener("message", handleBrowserElementSelected);
      channel.close();
    };
  }, [currentWorkspaceId]);

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

  const runGoalMutation = async (
    operation: () => Promise<unknown>,
    successMessage: string,
  ) => {
    setAttachmentError("");
    try {
      await operation();
      setComposerNotice(successMessage);
      setStatus(successMessage);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setAttachmentError(`Goal 操作失败：${message}`);
      throw error;
    }
  };

  const createGoal = async (objective: string) => {
    if (visibleGoal && goalNeedsReplacementConfirmation(visibleGoal.status)) {
      const confirmed = await confirm({
        title: "替换当前 Goal？",
        message: `当前 Goal“${visibleGoal.objective}”尚未完成。替换后将以新目标继续。`,
        confirmText: "替换 Goal",
        danger: true,
      });
      if (!confirmed) {
        setComposerNotice("已保留当前 Goal");
        return;
      }
    }

    let targetSession = state.currentSession;
    const targetWorkspaceId = currentWorkspaceId;
    if (!targetSession) {
      targetSession = await createSession(objective.slice(0, 80));
    }
    await runGoalMutation(
      () => updateGoal(
        { objective, status: "active", replace: true },
        { sessionId: targetSession.session_id, workspaceId: targetWorkspaceId },
      ),
      "Goal 已开始",
    );
  };

  const pauseGoal = async () => {
    if (!visibleGoal || visibleGoal.status !== "active") {
      throw new Error("只有进行中的 Goal 可以暂停");
    }
    await runGoalMutation(() => updateGoal({ status: "paused" }), "Goal 已暂停");
  };

  const resumeGoal = async () => {
    if (!visibleGoal || !goalCanResume(visibleGoal.status)) {
      throw new Error("当前 Goal 不可恢复");
    }
    await runGoalMutation(() => updateGoal({ status: "active" }), "Goal 已恢复");
  };

  const clearCurrentGoal = async (requireConfirmation: boolean) => {
    if (!visibleGoal) {
      throw new Error("当前会话没有 Goal");
    }
    if (requireConfirmation) {
      const confirmed = await confirm({
        title: "清除当前 Goal？",
        message: `将清除“${visibleGoal.objective}”的 Goal 状态。`,
        confirmText: "清除 Goal",
        danger: true,
      });
      if (!confirmed) {
        return;
      }
    }
    await runGoalMutation(clearGoal, "Goal 已清除");
  };

  const runGoalCommand = (args: string) => {
    const action = parseGoalSlashAction(args);
    const command = async () => {
      switch (action.kind) {
        case "show": {
          if (!state.currentSession) {
            setComposerNotice("当前新会话还没有 Goal；输入 /goal <目标> 开始");
            return;
          }
          const goal = await refreshGoal();
          setComposerNotice(goal
            ? `Goal：${GOAL_STATUS_LABELS[goal.status]} · ${goal.objective}`
            : "当前会话没有 Goal；输入 /goal <目标> 开始");
          return;
        }
        case "edit":
          if (!visibleGoal) {
            throw new Error("当前会话没有可编辑的 Goal");
          }
          setGoalEditOpen(true);
          return;
        case "pause":
          await pauseGoal();
          return;
        case "resume":
          await resumeGoal();
          return;
        case "clear":
          await clearCurrentGoal(false);
          return;
        case "create":
          await createGoal(action.objective);
      }
    };
    void command().catch((error: unknown) => {
      setAttachmentError(
        `Goal 操作失败：${error instanceof Error ? error.message : String(error)}`,
      );
    });
  };

  const {
    slashQuery,
    matchingSlashCommands,
    slashCommandMode,
    runSlashCommand,
    submitSlashInput,
  } = useComposerSlashCommands({
    input,
    currentSession: state.currentSession,
    compactLoading: state.compactLoading,
    getLatestAssistantContent,
    setInput,
    setAttachments,
    setAttachmentError,
    setComposerNotice,
    setAgentMenuOpen,
    setViewMenuOpen,
    setStatus,
    createSession: async (title) => {
      await createSession(title);
    },
    renameCurrentSession,
    switchContentView,
    compactSession,
    runGoalCommand,
  });

  useEffect(() => {
    setSlashCommandIndex(firstEnabledSlashCommandIndex(matchingSlashCommands));
  }, [matchingSlashCommands]);

  const handleSend = (deliveryPolicy: DeliveryPolicy = defaultDeliveryPolicy) => {
    if (submitSlashInput(slashCommandIndex)) {
      return;
    }

    const typedContent = input.trim();
    if (!typedContent && attachments.length === 0 && browserElements.length === 0) {
      return;
    }

    const elementContext = formatBrowserElementSelections(browserElements);
    const content = [typedContent || (attachments.length > 0 ? MEDIA_ONLY_PROMPT : ""), elementContext]
      .filter(Boolean)
      .join("\n\n");
    const sentAttachments = attachments;
    const sentBrowserElements = browserElements;
    setInput("");
    setAttachments([]);
    setBrowserElements([]);
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
      deliveryPolicy,
    ).catch((error: unknown) => {
      setInput(typedContent);
      setAttachments(sentAttachments);
      setBrowserElements(sentBrowserElements);
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
    setBrowserElements([]);
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

  const handleWorkspaceDefaultAgent = (agentId: string) => {
    void setWorkspaceDefaultAgent(agentId).catch(() => {
      // 错误状态和后端状态校准由 AppProvider 统一处理。
    });
  };

  const handleModelSelect = (providerId: string) => {
    setModelMenuOpen(false);
    void switchModel(providerId).catch(() => {
      // 错误状态和后端状态校准由 AppProvider 统一处理。
    });
  };

  const handleWorkspaceDefaultProvider = (providerId: string) => {
    void setWorkspaceDefaultProvider(currentAgent, providerId).catch(() => {
      // 错误状态和后端状态校准由 AppProvider 统一处理。
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

  const handleModelMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    setModelMenuOpen(false);
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
    if (e.altKey && showInterrupt) {
      handleSend(nextDeliveryPolicy(defaultDeliveryPolicy));
      return;
    }
    handleSend(defaultDeliveryPolicy);
  };

  return (
    <>
      <footer className="composer">
        <div className="composer-surface new-chat-input-container">
          <div className="new-chat-input-area">
            <div className="composer-copy sessions-chat-editor">
              <ComposerSlashCommandMenu
                query={slashQuery}
                commands={matchingSlashCommands}
                activeIndex={slashCommandIndex}
                anchorRef={textareaRef}
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
              {browserElements.length > 0 ? (
                <div className="composer-browser-elements" aria-label="已选择的浏览器元素">
                  {browserElements.map((element) => (
                    <div className="composer-browser-element" key={`${element.browserId}:${element.ref}`}>
                      <span className="codicon codicon-symbol-interface" aria-hidden="true" />
                      <span className="composer-browser-element-name" title={`${element.selector}\n${element.url}`}>
                        {`<${element.tag}> ${element.text || element.ref}`}
                      </span>
                      <button
                        type="button"
                        className="composer-attachment-remove"
                        aria-label={`移除页面元素 ${element.text || element.ref}`}
                        onClick={() => setBrowserElements((current) =>
                          current.filter((item) =>
                            item.browserId !== element.browserId || item.ref !== element.ref
                          )
                        )}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="composer-actions sessions-chat-toolbar">
              <div className="composer-actions-left">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept="*/*"
                  className="composer-file-input"
                  onChange={handleFileChange}
                />
                <button
                  id="attachButton"
                  type="button"
                  className="composer-icon-button sessions-chat-attach-button"
                  onClick={handleAttachClick}
                  title="添加附件"
                  aria-label="添加附件"
                >
                  +
                </button>
                <ComposerViewControl
                  controlRef={viewMenuRef}
                  currentView={currentView}
                  selectedView={state.contentView}
                  open={viewMenuOpen}
                  onToggle={() => setViewMenuOpen((open) => !open)}
                  onClose={() => setViewMenuOpen(false)}
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
                    onClose={() => setAgentMenuOpen(false)}
                    onSelect={handleAgentSelect}
                    onSetWorkspaceDefault={handleWorkspaceDefaultAgent}
                    onKeyDown={handleAgentMenuKeyDown}
                  />
                  <ComposerModelControl
                    controlRef={modelMenuRef}
                    providers={currentProviders}
                    currentProviderId={currentProviderId}
                    open={modelMenuOpen}
                    disabled={currentProviders.length === 0}
                    onToggle={() => setModelMenuOpen((open) => !open)}
                    onClose={() => setModelMenuOpen(false)}
                    onSelect={handleModelSelect}
                    onSetWorkspaceDefault={handleWorkspaceDefaultProvider}
                    onKeyDown={handleModelMenuKeyDown}
                  />
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
                    onSend={() => handleSend(defaultDeliveryPolicy)}
                    onAlternate={() => handleSend(nextDeliveryPolicy(defaultDeliveryPolicy))}
                    defaultDeliveryPolicy={defaultDeliveryPolicy}
                    onToggleDefault={() => {
                      const nextPolicy = nextDeliveryPolicy(defaultDeliveryPolicy);
                      void updateUiSettings({
                        layout: {
                          delivery_policy_default: nextPolicy,
                        },
                      }).catch((error: unknown) => {
                        setStatus(
                          `保存默认发送方式失败: ${error instanceof Error ? error.message : String(error)}`,
                        );
                      });
                    }}
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
      <WarmActionDialog
        open={goalEditOpen && visibleGoal !== null}
        title="编辑 Goal"
        description="编辑已完成的 Goal 会重新激活；预算仍不足时会继续保持受限，其他状态保持不变。"
        inputLabel="目标"
        initialValue={visibleGoal?.objective ?? ""}
        inputMaxLength={4000}
        inputMultiline
        confirmText="保存 Goal"
        onClose={() => setGoalEditOpen(false)}
        onConfirm={async (objective) => {
          if (!visibleGoal) {
            throw new Error("当前 Goal 已不存在");
          }
          await runGoalMutation(
            () => updateGoal({
              objective,
              status: goalEditStatus(visibleGoal.status),
            }),
            "Goal 已更新",
          );
        }}
      />
    </>
  );
}

export default React.memo(Composer);
