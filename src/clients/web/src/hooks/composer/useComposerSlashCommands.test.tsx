import React from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { COMPOSER_SLASH_COMMANDS, type SlashCommandOption } from "../../state/slashCommands";
import type { SessionCompactResult } from "../../types/backend";
import { useComposerSlashCommands } from "./useComposerSlashCommands";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, "clipboard", originalClipboard);
  } else {
    Reflect.deleteProperty(navigator, "clipboard");
  }
  restoreGlobalDescriptor("document", originalDocument);
});

/** 测试环境没有 DOM；用最小假 document 驱动 utils/clipboard 的兼容复制路径。 */
function installFakeDocument(): { copied: number } {
  const state = { copied: 0 };
  const textarea = {
    value: "",
    style: { position: "", left: "", top: "" },
    setAttribute: () => undefined,
    focus: () => undefined,
    select: () => undefined,
    remove: () => undefined,
  };
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      createElement: () => textarea,
      body: { appendChild: () => undefined },
      execCommand: (command: string) => {
        if (command === "copy") state.copied += 1;
        return true;
      },
    },
  });
  return state;
}

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

  test("创建失败时写入可见错误而不是变成未处理 rejection", async () => {
    let runSlashCommand:
      | ((command: SlashCommandOption, args?: string) => void)
      | undefined;
    let attachmentError = "";

    function Harness() {
      ({ runSlashCommand } = useComposerSlashCommands({
        input: "/new",
        currentSession: null,
        compactLoading: false,
        getLatestAssistantContent: () => null,
        setInput: () => undefined,
        setAttachments: () => undefined,
        setAttachmentError: (update) => {
          attachmentError = typeof update === "function"
            ? update(attachmentError)
            : update;
        },
        setComposerNotice: () => undefined,
        setAgentMenuOpen: () => undefined,
        setViewMenuOpen: () => undefined,
        setStatus: () => undefined,
        createSession: async () => {
          throw new Error("请求失败 500 : 工作区不可用");
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

    let renderer: ReturnType<typeof create> | undefined;
    await act(async () => {
      renderer = create(<Harness />);
    });
    const newCommand = COMPOSER_SLASH_COMMANDS.find(
      (command) => command.id === "new",
    );
    if (!newCommand || !runSlashCommand) {
      throw new Error("测试未找到 /new 命令执行器");
    }

    // 未接住 rejection 会让 bun test 以 Unhandled error 终止本文件。
    await act(async () => {
      runSlashCommand?.(newCommand);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(attachmentError).toContain("创建会话失败");
    expect(attachmentError).toContain("请求失败 500 : 工作区不可用");
    renderer?.unmount();
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

describe("Composer /copy 命令的剪贴板唯一实现", () => {
  test("Clipboard API 不可用时改走兼容复制并提示成功", async () => {
    // 非安全上下文里 navigator.clipboard 真实缺失；兼容复制只能由
    // utils/clipboard 的实现承担，hook 不得再持有第二套 execCommand 样板。
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const documentState = installFakeDocument();
    let copyCommand:
      | ((command: SlashCommandOption, args?: string) => void)
      | undefined;
    let notice = "";

    function Harness() {
      ({ runSlashCommand: copyCommand } = useComposerSlashCommands({
        input: "/copy",
        currentSession: null,
        compactLoading: false,
        getLatestAssistantContent: () => "最近一条助手回复",
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
        compactSession: async () => {
          throw new Error("/copy 测试不会执行压缩");
        },
        runGoalCommand: () => undefined,
      }));
      return null;
    }

    let renderer: ReturnType<typeof create> | undefined;
    await act(async () => {
      renderer = create(<Harness />);
    });
    const copySlashCommand = COMPOSER_SLASH_COMMANDS.find(
      (command) => command.id === "copy",
    );
    if (!copySlashCommand || !copyCommand) {
      throw new Error("测试未找到 /copy 命令执行器");
    }

    await act(async () => {
      copyCommand?.(copySlashCommand);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(documentState.copied).toBe(1);
    expect(notice).toBe("已复制最近助手回复");
    renderer?.unmount();
  });

  test("document.execCommand 兼容复制在全仓只有 utils/clipboard 一处实现", async () => {
    const sourceRoot = Bun.fileURLToPath(new URL("../../", import.meta.url));
    const hits: string[] = [];
    const glob = new Bun.Glob("**/*.{ts,tsx}");

    for await (const relativePath of glob.scan({ cwd: sourceRoot })) {
      // 守卫只约束产品源码：测试可以合法地桩掉 execCommand 来验证兼容路径。
      if (/\.test\.tsx?$/.test(relativePath)) {
        continue;
      }
      const source = await Bun.file(`${sourceRoot}/${relativePath}`).text();
      if (source.includes('execCommand("copy")')) {
        hits.push(relativePath);
      }
    }

    // 唯一实现被复制回任何 hook/组件，这里都会变红。
    expect(hits).toEqual(["utils/clipboard.ts"]);
  });
});
