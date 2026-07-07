import { useCallback, useMemo, type Dispatch, type SetStateAction } from "react";
import {
  COMPOSER_SLASH_COMMANDS,
  getSlashCommandArgs,
  matchingSlashCommands,
  slashQueryFromInput,
  type SlashCommandOption,
} from "../state/slashCommands";
import type { AppState, ConversationContentView } from "../types/frontend";
import type { SelectedAttachment } from "../utils/mediaAttachments";

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

export function useComposerSlashCommands({
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
  renameCurrentSession,
  switchContentView,
  compactSession,
}: {
  input: string;
  state: AppState;
  setInput: Dispatch<SetStateAction<string>>;
  setAttachments: Dispatch<SetStateAction<SelectedAttachment[]>>;
  setAttachmentError: Dispatch<SetStateAction<string>>;
  setComposerNotice: Dispatch<SetStateAction<string>>;
  setAgentMenuOpen: Dispatch<SetStateAction<boolean>>;
  setViewMenuOpen: Dispatch<SetStateAction<boolean>>;
  setStatus: (text: string) => void;
  createSession: (title?: string) => Promise<void>;
  renameCurrentSession: (inlineTitle: string) => void;
  switchContentView: (view: ConversationContentView) => void;
  compactSession: () => Promise<void>;
}) {
  const slashCommands = COMPOSER_SLASH_COMMANDS;
  const slashQuery = useMemo(() => slashQueryFromInput(input), [input]);
  const matchedSlashCommands = useMemo(
    () => matchingSlashCommands(slashCommands, slashQuery),
    [slashCommands, slashQuery],
  );
  const slashCommandMode = slashQuery !== null;

  const runSlashCommand = useCallback(
    (command: SlashCommandOption, args = "") => {
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
          switchContentView("events");
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
          switchContentView("default");
          break;
        case "events":
          switchContentView("events");
          break;
        case "requests":
          switchContentView("requests");
          break;
        case "resources":
          switchContentView("resources");
          break;
        case "state":
          switchContentView("agent");
          break;
        case "compact":
          if (state.currentSession && !state.compactLoading) {
            void compactSession();
          }
          break;
        default:
          break;
      }
    },
    [
      compactSession,
      createSession,
      renameCurrentSession,
      setAgentMenuOpen,
      setAttachmentError,
      setAttachments,
      setComposerNotice,
      setInput,
      setStatus,
      setViewMenuOpen,
      state.compactLoading,
      state.currentSession,
      state.messages,
      switchContentView,
    ],
  );

  const submitSlashInput = useCallback(
    (activeIndex: number) => {
      if (!slashCommandMode) {
        return false;
      }

      const command =
        matchedSlashCommands[activeIndex] ??
        matchedSlashCommands[0];
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
    },
    [
      input,
      matchedSlashCommands,
      runSlashCommand,
      setAttachmentError,
      setComposerNotice,
      setInput,
      slashCommandMode,
      slashQuery,
    ],
  );

  return {
    slashCommands,
    slashQuery,
    matchingSlashCommands: matchedSlashCommands,
    slashCommandMode,
    runSlashCommand,
    submitSlashInput,
  };
}
