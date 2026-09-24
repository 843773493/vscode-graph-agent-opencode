import { useCallback, useMemo, type Dispatch, type SetStateAction } from "react";
import {
  COMPOSER_SLASH_COMMANDS,
  getSlashCommandArgs,
  matchingSlashCommands,
  slashQueryFromInput,
  type SlashCommandOption,
} from "../../state/slashCommands";
import type { ConversationContentView } from "../../types/frontend";
import type { Session, SessionCompactResult } from "../../types/backend";
import type { SelectedAttachment } from "../../utils/media/mediaAttachments";
import { errorMessage } from "../../utils/errorMessage";
import { copyTextToClipboard } from "../../utils/clipboard";

export function useComposerSlashCommands({
  input,
  currentSession,
  compactLoading,
  getLatestAssistantContent,
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
  runGoalCommand,
}: {
  input: string;
  currentSession: Session | null;
  compactLoading: boolean;
  getLatestAssistantContent: () => string | null;
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
  compactSession: () => Promise<SessionCompactResult>;
  runGoalCommand: (args: string) => void;
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
          void createSession(args.trim() || undefined).catch((error: unknown) => {
            // 与 /compact 分支同范式：失败写进 Composer 可见错误区，并接住
            // rejection 避免未处理拒绝。AppProvider 也会把同一条失败写进
            // AppState.status，状态栏同样会显示。
            setAttachmentError(
              `创建会话失败：${errorMessage(error)}`,
            );
          });
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
          const latestAssistantContent = getLatestAssistantContent();
          if (!latestAssistantContent) {
            setAttachmentError("没有可复制的助手回复");
            break;
          }
          // 剪贴板写入只走 utils/clipboard 的唯一实现：Clipboard API 与兼容复制的
          // 取舍、非安全上下文回退都在那里收口，这里不重复造第二套。
          void copyTextToClipboard(latestAssistantContent)
            .then(() => {
              setStatus("已复制最近助手回复");
              setComposerNotice("已复制最近助手回复");
            })
            .catch((error: unknown) => {
              setAttachmentError(
                `复制失败：${errorMessage(error)}`,
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
        case "changes":
          switchContentView("changes");
          break;
        case "state":
          switchContentView("agent");
          break;
        case "compact":
          if (currentSession && !compactLoading) {
            void compactSession()
              .then((result) => {
                setComposerNotice(result.status === "scheduled"
                  ? "已安排上下文压缩，将在下一条消息发送前执行"
                  : result.status === "compacted"
                    ? `已压缩上下文：${result.summarized_message_count} 条消息`
                    : `上下文未压缩：${result.message}`);
              })
              .catch((error: unknown) => {
                setAttachmentError(
                  `上下文压缩失败：${errorMessage(error)}`,
                );
              });
          }
          break;
        case "goal":
          runGoalCommand(args);
          break;
        default:
          break;
      }
    },
    [
      compactSession,
      createSession,
      renameCurrentSession,
      runGoalCommand,
      setAgentMenuOpen,
      setAttachmentError,
      setAttachments,
      setComposerNotice,
      setInput,
      setStatus,
      setViewMenuOpen,
      compactLoading,
      currentSession,
      getLatestAssistantContent,
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
