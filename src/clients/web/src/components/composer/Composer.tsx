import { t } from "../../../../shared/core/i18n/index.js";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import { MessageSubmissionRejectedError, MessageSubmissionUnknownError } from "../../api/session/sessionMessages";
import { useComposerState } from "../../hooks/app/appContext";
import { useComposerSlashCommands } from "../../hooks/composer/useComposerSlashCommands";
import { useComposerAttachments, useComposerDraft, writeComposerAttachments, type ComposerDraftController } from "../../hooks/composer/useComposerDraft";
import { useSkillUntrackCommand } from "../../hooks/composer/useSkillUntrackCommand";
import {
  composerDraftScopeKey,
  composerDraftExceedsLimit,
  writeComposerDraft,
} from "../../state/composerDrafts/storage";
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
} from "../../utils/media/mediaAttachments";
import ComposerActionButtons from "./ComposerActionButtons";
import ComposerAgentControl from "./ComposerAgentControl";
import ComposerModelControl from "./ComposerModelControl";
import ComposerAttachmentTray from "./ComposerAttachmentTray";
import MessageSubmissionRecovery from "./MessageSubmissionRecovery";
import ComposerSlashCommandMenu from "./ComposerSlashCommandMenu";
import ComposerToolControl from "./ComposerToolControl";
import ComposerViewControl from "./ComposerViewControl";
import SessionNameDialog from "../overlays/SessionNameDialog";
import WarmActionDialog from "../overlays/WarmActionDialog";
import { useWarmConfirm } from "../shell/WarmConfirmProvider";
import {
  formatBrowserElementSelections,
  parseBrowserElementSelectionBundle,
  type BrowserElementSelection,
} from "../../utils/selection/browserElementSelection";
import {
  GOAL_STATUS_LABELS,
  goalCanResume,
  goalEditStatus,
  goalNeedsReplacementConfirmation,
  parseGoalSlashAction,
} from "../../state/sessionGoal";
import { errorMessage } from "../../utils/errorMessage";

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

function Composer({ threadId, onDraftReady, onManageConfiguration }: { threadId?: string; onDraftReady?: (controller: ComposerDraftController | undefined) => void;
  onManageConfiguration?: (kind: 'providers' | 'agents', id: string) => void }) {
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
    refreshAgents,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
    switchContentView,
    createSession,
    renameSession,
    updateUiSettings,
    getLatestAssistantContent,
    refreshTurnHistory,
  } =
    useComposerState();
  const [selectedBrowserElements, setSelectedBrowserElements] = useState<
    BrowserElementSelection[]
  >([]);
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
  // 同一份输入内容的重入闸门。产品明确支持「生成中继续排队下一条消息」
  // （见 composerHint：「正在生成，可继续发送下一条或点击停止」），因此不能
  // 禁用发送或整个会话级加锁——那会一并挡掉合法的排队。这里只挡「同一次提交
  // 被瞬时重复触发」：Enter 提交会同步 setInput("")，但同一事件循环内的
  // 重复触发仍可能读到尚未刷新的旧 input，导致同一 payload 被提交两次。
  // 该身份在每次提交同步建立、按 payload 去重，成功的同内容再提交不会被误挡，
  // 失败回填后也会被清空以允许重试。
  const inFlightSubmissionRef = useRef<{ content: string; policy: DeliveryPolicy } | null>(null);
  const agentMenuRef = useRef<HTMLDivElement | null>(null);
  const modelMenuRef = useRef<HTMLDivElement | null>(null);
  const currentSessionId = state.currentSession?.session_id ?? null;
  const currentWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  const sessionKey = currentSessionId && currentWorkspaceId
    ? sessionScopeKey(currentWorkspaceId, currentSessionId) : currentSessionId;
  const currentSessionCacheKey = threadId === undefined ? sessionKey
    : JSON.stringify([currentWorkspaceId, currentSessionId, threadId]);
  const [input, setInput, draftController] = useComposerDraft(
    currentWorkspaceId,
    currentSessionId,
    state.browserViewScope,
    threadId,
    setAttachmentError,
  );
  const draftScopeKey = composerDraftScopeKey(currentWorkspaceId, currentSessionId, state.browserViewScope, threadId);
  useEffect(() => {
    onDraftReady?.(draftController);
    return () => onDraftReady?.(undefined);
  }, [onDraftReady, draftController]);
  const [attachments, setAttachments] = useComposerAttachments(draftScopeKey, setAttachmentError);
  // 最近一次渲染的会话作用域键。提交失败回填必须按「发起提交时的作用域」归属，
  // 否则用户在发送在途期间切走会话后，失败回填会把上一个会话的输入写进新会话。
  const currentScopeKeyRef = useRef(currentSessionCacheKey);
  currentScopeKeyRef.current = currentSessionCacheKey;
  const previousSessionIdRef = useRef<string | null>(currentSessionCacheKey);
  const visibleGoal = state.currentGoalSessionId === currentSessionId
    ? state.currentGoal
    : null;

  const hasContent = input.trim().length > 0
    || attachments.length > 0
    || selectedBrowserElements.length > 0;
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
        ? t("composer.queued", queuedCount)
        : t("composer.generating");
    }
    return t("composer.sendHint");
  }, [queuedCount, showInterrupt]);

  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [input]);

  useEffect(() => {
    if (previousSessionIdRef.current === currentSessionCacheKey) {
      return;
    }
    previousSessionIdRef.current = currentSessionCacheKey;
    setSelectedBrowserElements([]);
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
      const selectionBundle = parseBrowserElementSelectionBundle(value);
      if (!selectionBundle || selectionBundle.workspaceId !== currentWorkspaceId) {
        return;
      }
      setSelectedBrowserElements((currentSelections) => {
        const retainedSelections = currentSelections.filter(
          (selection) => selection.browserId !== selectionBundle.browserId,
        );
        const latestSelectionByRef = new Map<string, BrowserElementSelection>();
        // 解析器已将相同 browserId 绑定到 bundle 内全部元素，因此按 ref 合并即可。
        for (const selection of selectionBundle.elements) {
          // 删除再写入可保留重复 ref 的最后一个载荷及其最后出现顺序。
          latestSelectionByRef.delete(selection.ref);
          latestSelectionByRef.set(selection.ref, selection);
        }
        return [...retainedSelections, ...latestSelectionByRef.values()];
      });
      const lastSelection = selectionBundle.elements[selectionBundle.elements.length - 1];
      setComposerNotice(
        selectionBundle.mode === "rich"
          ? t("composer.richElementSelected", lastSelection.tag)
          : t("composer.elementSelected", lastSelection.tag),
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
      setAttachmentError(t("composer.noSessionToName"));
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
        setComposerNotice(t("composer.renamed", title));
      })
      .catch((error: unknown) => {
        setAttachmentError(
          t("composer.renameFailed", errorMessage(error)),
        );
      });
  };

  const submitRenameDialog = (title: string) => {
    const session = state.currentSession;
    if (!session) {
      setRenameDialogError(t("composer.noSessionToName"));
      return;
    }

    setRenameDialogSubmitting(true);
    setRenameDialogError(null);
    void renameSession(session.session_id, title)
      .then(() => {
        setRenameDialogOpen(false);
        setComposerNotice(t("composer.renamed", title));
      })
      .catch((error: unknown) => {
        setRenameDialogError(
          errorMessage(error),
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
    setComposerNotice("");
    try {
      await operation();
      setStatus(successMessage);
    } catch (error) {
      const message = errorMessage(error);
      setAttachmentError(t("composer.goalFailed", message));
      throw error;
    }
  };

  const createGoal = async (objective: string) => {
    if (visibleGoal && goalNeedsReplacementConfirmation(visibleGoal.status)) {
      const confirmed = await confirm({
        title: t("composer.replaceGoal"),
        message: t("composer.replaceGoalDetail", visibleGoal.objective),
        confirmText: t("composer.replaceGoalConfirm"),
        danger: true,
      });
      if (!confirmed) {
        setComposerNotice(t("composer.goalKept"));
        return;
      }
    }

    let targetSession = state.currentSession;
    const targetWorkspaceId = currentWorkspaceId;
    if (!targetSession) {
      targetSession = await createSession(objective.slice(0, 80));
    }
    const targetThreadId = targetSession.thread_id;
    if (!targetThreadId) throw new Error("Goal 目标会话缺少 main Thread 身份");
    await runGoalMutation(
      () => updateGoal(
        {
          objective, status: "active", replace: true,
          expected_control_revision: targetSession.session_id === state.currentSession?.session_id
            ? state.currentGoalControlRevision : 0,
        },
        { sessionId: targetSession.session_id, threadId: targetThreadId, workspaceId: targetWorkspaceId },
      ),
      t("composer.goalStarted"),
    );
  };

  const pauseGoal = async () => {
    if (!visibleGoal || visibleGoal.status !== "active") {
      throw new Error(t("composer.goalPauseInvalid"));
    }
    await runGoalMutation(() => updateGoal({ status: "paused", expected_control_revision: visibleGoal.control_revision }), t("composer.goalPaused"));
  };

  const resumeGoal = async () => {
    if (!visibleGoal || !goalCanResume(visibleGoal.status)) {
      throw new Error(t("composer.goalResumeInvalid"));
    }
    await runGoalMutation(() => updateGoal({ status: "active", expected_control_revision: visibleGoal.control_revision }), t("composer.goalResumed"));
  };

  const clearCurrentGoal = async (requireConfirmation: boolean) => {
    if (!visibleGoal) {
      throw new Error(t("composer.goalMissing"));
    }
    if (requireConfirmation) {
      const confirmed = await confirm({
        title: t("composer.clearGoal"),
        message: t("composer.clearGoalDetail", visibleGoal.objective),
        confirmText: t("composer.clearGoalConfirm"),
        danger: true,
      });
      if (!confirmed) {
        return;
      }
    }
    await runGoalMutation(() => clearGoal({
      sessionId: visibleGoal.session_id, threadId: visibleGoal.thread_id,
      workspaceId: currentWorkspaceId,
    }, visibleGoal.control_revision), t("composer.goalCleared"));
  };

  const runGoalCommand = (args: string) => {
    const action = parseGoalSlashAction(args);
    const command = async () => {
      switch (action.kind) {
        case "show": {
          if (!state.currentSession) {
            setComposerNotice(t("composer.newGoalHint"));
            return;
          }
          const goal = await refreshGoal();
          setComposerNotice(goal
            ? t("composer.goalSummary", GOAL_STATUS_LABELS[goal.status], goal.objective)
            : t("composer.goalHint"));
          return;
        }
        case "edit":
          if (!visibleGoal) {
            throw new Error(t("composer.goalNotEditable"));
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
        t("composer.goalCommandFailed", errorMessage(error)),
      );
    });
  };

  const runSkillUntrackCommand = useSkillUntrackCommand({
    state,
    sessionId: currentSessionId,
    workspaceId: currentWorkspaceId,
    scopeKey: currentSessionCacheKey,
    setNotice: setComposerNotice,
    setError: setAttachmentError,
  });

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
    runSkillUntrackCommand,
  });

  useEffect(() => {
    setSlashCommandIndex(firstEnabledSlashCommandIndex(matchingSlashCommands));
  }, [matchingSlashCommands]);

  const handleSend = (deliveryPolicy: DeliveryPolicy = defaultDeliveryPolicy) => {
    if (submitSlashInput(slashCommandIndex)) {
      return;
    }

    const typedContent = input.trim();
    if (!typedContent && attachments.length === 0 && selectedBrowserElements.length === 0) {
      return;
    }

    const elementContext = formatBrowserElementSelections(selectedBrowserElements);
    const content = [typedContent || (attachments.length > 0 ? MEDIA_ONLY_PROMPT : ""), elementContext]
      .filter(Boolean)
      .join("\n\n");
    // 同步重入守卫：同一 payload 的提交尚在进行时，重复触发直接返回，不重复
    // 清空/发送/反馈。仅按 payload 身份去重，排队下一条不同内容不受影响。
    const inFlight = inFlightSubmissionRef.current;
    if (inFlight && inFlight.content === content && inFlight.policy === deliveryPolicy) {
      return;
    }
    const submission = {
      content,
      policy: deliveryPolicy,
      scopeKey: currentSessionCacheKey,
      draftScopeKey: composerDraftScopeKey(
        currentWorkspaceId,
        currentSessionId,
        state.browserViewScope,
        threadId,
      ),
    };
    inFlightSubmissionRef.current = submission;
    const sentAttachments = attachments;
    const sentBrowserElementSelections = selectedBrowserElements;
    setInput("");
    setAttachments([]);
    setSelectedBrowserElements([]);
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
      if (inFlightSubmissionRef.current === submission) {
        inFlightSubmissionRef.current = null;
      }
      if (error instanceof MessageSubmissionUnknownError || error instanceof MessageSubmissionRejectedError) {
        if (currentScopeKeyRef.current === submission.scopeKey) setAttachmentError(error.message);
        return;
      }
      if (currentScopeKeyRef.current === submission.scopeKey) {
        // 只填空输入：发送在途期间用户可能已经写下下一条内容，失败回填绝不能
        // 覆盖它。空输入时才把未发出的内容恢复回来，避免用户的草稿凭空消失。
        setInput((current) => (current ? current : typedContent));
        setAttachments((current) => (current.length > 0 ? current : sentAttachments));
        setSelectedBrowserElements((currentSelections) => (
          currentSelections.length > 0 ? currentSelections : sentBrowserElementSelections
        ));
        setAttachmentError(
          t("composer.sendFailed", errorMessage(error)),
        );
        return;
      }
      // 用户已切走会话：把未发出的输入写回发起会话自己的持久草稿，避免把它
      // 污染成当前会话的输入，同时用户切回该会话时内容仍在。失败原因已由
      // AppProvider 写入 AppState.status，不会静默消失。
      if (!composerDraftExceedsLimit(typedContent)) {
        void Promise.all([
          writeComposerDraft(submission.draftScopeKey, typedContent),
          writeComposerAttachments(submission.draftScopeKey, sentAttachments),
        ]).catch(cause => setStatus(t("composer.draftRestoreFailed", errorMessage(cause))));
      }
    }).then(() => {
      if (inFlightSubmissionRef.current === submission) {
        inFlightSubmissionRef.current = null;
      }
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
        ? [errorMessage(result.reason)]
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
    setSelectedBrowserElements([]);
    setAttachmentError("");
    setComposerNotice("");
  };

  const handleInterrupt = () => {
    if (!showInterrupt) {
      return;
    }

    setAttachmentError("");
    const scopeKey = currentSessionCacheKey;
    void interruptSession().catch((error: unknown) => {
      if (currentScopeKeyRef.current === scopeKey) setAttachmentError(t("composer.interruptFailed", errorMessage(error)));
    });
  };

  const handleViewSelect = (view: ConversationContentView) => {
    setViewMenuOpen(false);
    void switchContentView(view);
  };

  const handleAgentSelect = (agentId: string) => {
    setAgentMenuOpen(false);
    setAttachmentError("");
    void switchAgent(agentId).catch((error: unknown) => {
      // 切换失败必须让用户看得见：写进 Composer 本地错误区，同时 AppProvider
      // 已把同一条失败写进 AppState.status，状态栏也会显示。
      setAttachmentError(
        t("composer.agentFailed", errorMessage(error)),
      );
    });
  };

  const handleWorkspaceDefaultAgent = (agentId: string) => {
    setAttachmentError("");
    void setWorkspaceDefaultAgent(agentId).catch((error: unknown) => {
      setAttachmentError(
        t("composer.agentDefaultFailed", errorMessage(error)),
      );
    });
  };

  const handleModelSelect = (providerId: string) => {
    setModelMenuOpen(false);
    setAttachmentError("");
    void switchModel(providerId).catch((error: unknown) => {
      setAttachmentError(
        t("composer.modelFailed", errorMessage(error)),
      );
    });
  };

  const handleModelMenuToggle = () => {
    const nextOpen = !modelMenuOpen;
    setModelMenuOpen(nextOpen);
    if (!nextOpen || !currentWorkspaceId) {
      return;
    }
    void refreshAgents(currentWorkspaceId).catch((error: unknown) => {
      setAttachmentError(t("composer.refreshModelsFailed", errorMessage(error)));
    });
  };

  const handleWorkspaceDefaultProvider = (providerId: string) => {
    setAttachmentError("");
    void setWorkspaceDefaultProvider(currentAgent, providerId).catch((error: unknown) => {
      setAttachmentError(
        t("composer.modelDefaultFailed", errorMessage(error)),
      );
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
        <MessageSubmissionRecovery port={state.apiPort ?? DEFAULT_BACKEND_PORT}
          owner={currentWorkspaceId && currentSessionId && (threadId ?? state.currentSession?.thread_id)
            ? { workspaceKey: currentWorkspaceId, sessionId: currentSessionId, threadId: (threadId ?? state.currentSession!.thread_id)! } : null}
          onAccepted={refreshTurnHistory} />
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
                data-testid="composer-input"
                placeholder={t("composer.placeholder")}
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
              {visibleGoal ? (
                <div
                  className="composer-goal-status"
                  role="status"
                  aria-label={t("composer.currentGoal", GOAL_STATUS_LABELS[visibleGoal.status], visibleGoal.objective)}
                  title={visibleGoal.objective}
                  data-testid="composer-current-goal"
                >
                  <span className="codicon codicon-target" aria-hidden="true" />
                  <span className="composer-goal-status-label">
                    Goal · {GOAL_STATUS_LABELS[visibleGoal.status]}
                  </span>
                  <span className="composer-goal-status-objective">
                    {visibleGoal.objective}
                  </span>
                </div>
              ) : state.goalLoading && currentSessionId ? (
                <div className="composer-goal-status" role="status">
                  {t("composer.loadingGoal")}
                </div>
              ) : null}
              {state.goalError && currentSessionId ? (
                <div className="composer-attachment-error" role="alert">
                  {t("composer.syncGoalFailed", state.goalError)}
                </div>
              ) : null}
              {state.sessionExecutionSyncError && currentSessionId ? (
                <div className="composer-attachment-error" role="alert">
                  {t("composer.syncExecutionFailed", state.sessionExecutionSyncError)}
                </div>
              ) : null}
              {selectedBrowserElements.length > 0 ? (
                <div className="composer-browser-elements" aria-label={t("composer.selectedElements")}>
                  {selectedBrowserElements.map((element) => (
                    <div className="composer-browser-element" key={`${element.browserId}:${element.ref}`}>
                      <span className="codicon codicon-symbol-interface" aria-hidden="true" />
                      <span className="composer-browser-element-name" title={`${element.selector}\n${element.url}`}>
                        {`<${element.tag}> ${element.text || element.ref}`}
                      </span>
                      <button
                        type="button"
                        className="composer-attachment-remove"
                        aria-label={t("composer.removeElement", element.text || element.ref)}
                        onClick={() => setSelectedBrowserElements((currentSelections) =>
                          currentSelections.filter((selection) =>
                            selection.browserId !== element.browserId || selection.ref !== element.ref
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
                  title={t("composer.attach")}
                  aria-label={t("composer.attach")}
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
                    onManage={onManageConfiguration ? () => { setAgentMenuOpen(false); onManageConfiguration('agents', currentAgent); } : undefined}
                  />
                  <ComposerModelControl
                    controlRef={modelMenuRef}
                    providers={currentProviders}
                    currentProviderId={currentProviderId}
                    open={modelMenuOpen}
                    disabled={currentProviders.length === 0}
                    onToggle={handleModelMenuToggle}
                    onClose={() => setModelMenuOpen(false)}
                    onSelect={handleModelSelect}
                    onSetWorkspaceDefault={handleWorkspaceDefaultProvider}
                    onKeyDown={handleModelMenuKeyDown}
                    onManage={onManageConfiguration ? () => { setModelMenuOpen(false); onManageConfiguration('providers', currentProviderId); } : undefined}
                  />
                  <ComposerToolControl
                    apiPort={state.apiPort ?? DEFAULT_BACKEND_PORT}
                    agentId={currentAgent}
                    workspaceId={currentWorkspaceId}
                    onStatus={setStatus}
                    providers={currentProviders}
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
                        setAttachmentError(
                          t("composer.saveDeliveryFailed", errorMessage(error)),
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
        title={t("composer.nameSession")}
        label={t("composer.sessionName")}
        initialValue={state.currentSession?.title || t("composer.newSession")}
        confirmText={t("composer.saveName")}
        submitting={renameDialogSubmitting}
        error={renameDialogError}
        onCancel={closeRenameDialog}
        onSubmit={submitRenameDialog}
      />
      <WarmActionDialog
        open={goalEditOpen && visibleGoal !== null}
        title={t("composer.editGoal")}
        description={t("composer.editGoalDescription")}
        inputLabel={t("composer.objective")}
        initialValue={visibleGoal?.objective ?? ""}
        inputMaxLength={4000}
        inputMultiline
        confirmText={t("composer.saveGoal")}
        onClose={() => setGoalEditOpen(false)}
        onConfirm={async (objective) => {
          if (!visibleGoal) {
            throw new Error(t("composer.goalGone"));
          }
          await runGoalMutation(
            () => updateGoal({
              objective,
              status: goalEditStatus(visibleGoal.status),
              expected_control_revision: visibleGoal.control_revision,
            }),
            t("composer.goalUpdated"),
          );
        }}
      />
    </>
  );
}

export default React.memo(Composer);
