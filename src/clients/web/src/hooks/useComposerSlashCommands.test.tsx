import React from "react";
import { describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { COMPOSER_SLASH_COMMANDS, type SlashCommandOption } from "../state/slashCommands";
import type { SessionCompactResult } from "../types/backend";
import { useComposerSlashCommands } from "./useComposerSlashCommands";

describe("Composer /new 命令", () => {
  test("没有标题时直接创建后端会话", () => {
    const createdTitles: Array<string | undefined> = [];
    let runSlashCommand:
      | ((command: SlashCommandOption, args?: string) => void)
      | undefined;

    function Harness() {
      ({ runSlashCommand } = useComposerSlashCommands({
        input: "/new",
        currentSession: null,
        compactLoading: false,
        getLatestAssistantContent: () => null,
        setInput: () => undefined,
        setAttachments: () => undefined,
        setAttachmentError: () => undefined,
        setComposerNotice: () => undefined,
        setAgentMenuOpen: () => undefined,
        setViewMenuOpen: () => undefined,
        setStatus: () => undefined,
        createSession: async (title) => {
          createdTitles.push(title);
        },
        renameCurrentSession: () => undefined,
        switchContentView: () => undefined,
        compactSession: async () => {
          throw new Error("/new 测试不会执行压缩");
        },
        runGoalCommand: () => undefined,
      }));
      return null;
    }

    act(() => {
      create(<Harness />);
    });
    const newCommand = COMPOSER_SLASH_COMMANDS.find(
      (command) => command.id === "new",
    );
    if (!newCommand || !runSlashCommand) {
      throw new Error("测试未找到 /new 命令执行器");
    }

    act(() => {
      runSlashCommand?.(newCommand);
    });

    expect(createdTitles).toEqual([undefined]);
  });
});

describe("Composer /compact 命令", () => {
  test("成功后在编辑器中显示服务端返回的压缩状态", async () => {
    let runSlashCommand:
      | ((command: SlashCommandOption, args?: string) => void)
      | undefined;
    let notice = "";
    const result: SessionCompactResult = {
      session_id: "session",
      status: "scheduled",
      message: "将在下一次完整模型请求前执行",
      before_message_count: 12,
      effective_message_count_before: 12,
      effective_message_count_after: 12,
      summarized_message_count: 0,
      retained_message_count: 12,
      summary: null,
      history_file_path: null,
      strategy: null,
      compacted_at: null,
    };

    function Harness() {
      ({ runSlashCommand } = useComposerSlashCommands({
        input: "/compact",
        currentSession: {
          session_id: "session",
          workspace_id: "workspace",
          title: "压缩测试",
          current_agent_id: "default",
          created_at: "2026-07-28T00:00:00Z",
          updated_at: "2026-07-28T00:00:00Z",
        },
        compactLoading: false,
        getLatestAssistantContent: () => null,
        setInput: () => undefined,
        setAttachments: () => undefined,
        setAttachmentError: () => undefined,
        setComposerNotice: (update) => {
          notice = typeof update === "function" ? update(notice) : update;
        },
        setAgentMenuOpen: () => undefined,
        setViewMenuOpen: () => undefined,
        setStatus: () => undefined,
        createSession: async () => undefined,
        renameCurrentSession: () => undefined,
        switchContentView: () => undefined,
        compactSession: async () => result,
        runGoalCommand: () => undefined,
      }));
      return null;
    }

    let renderer: ReturnType<typeof create> | undefined;
    await act(async () => {
      renderer = create(<Harness />);
    });
    const compactCommand = COMPOSER_SLASH_COMMANDS.find(
      (command) => command.id === "compact",
    );
    if (!compactCommand || !runSlashCommand) {
      throw new Error("测试未找到 /compact 命令执行器");
    }

    await act(async () => {
      runSlashCommand?.(compactCommand);
      await Promise.resolve();
    });

    expect(notice).toBe("已安排上下文压缩，将在下一条消息发送前执行");
    renderer?.unmount();
  });
});
