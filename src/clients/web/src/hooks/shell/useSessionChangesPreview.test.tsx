import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type {
  Session,
  SessionChangeset,
  SessionFileChange,
} from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";
import { useSessionChangesPreview } from "./useSessionChangesPreview";

/**
 * 会话文件变更展示编排的契约：侧边栏「更改」标签下必须先把右侧栏展开，
 * 打开变更文件预览要按 changeset+文件+审阅状态去重，切换变更视图只在需要时发生。
 */

const mountedRenderers: ReactTestRenderer[] = [];

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
});

function session(): Session {
  return {
    session_id: "ses-1",
    workspace_id: "ws-1",
    title: "标题",
    current_agent_id: "default",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  } as Session;
}

function file(filePath: string, reviewed = false): SessionFileChange {
  return { file_path: filePath, reviewed } as SessionFileChange;
}

function changeset(files: SessionFileChange[]): SessionChangeset {
  return {
    changeset_id: "cs-1",
    session_id: "ses-1",
    is_default: true,
    files,
    summary: { files: files.length, additions: 1, deletions: 0 },
  } as unknown as SessionChangeset;
}

interface MountOptions {
  contentView?: ConversationContentView;
  activeChangeset?: SessionChangeset | null;
  auxiliaryVisible?: boolean;
  auxiliaryTab?: "files" | "changes" | "debug" | "resources";
  layoutAuxiliaryVisible?: boolean | undefined;
  activePreviewPath?: string | null;
  sessionChangesLoading?: boolean;
  sessionChangesError?: string | null;
}

function mountHook(options: MountOptions = {}) {
  const openedPreviews: Array<[string, string]> = [];
  const switches: ConversationContentView[] = [];
  const auxiliaryVisibleWrites: boolean[] = [];
  const statuses: string[] = [];
  let hook: ReturnType<typeof useSessionChangesPreview> | undefined;
  // 允许重渲染推进 props：去重键的鉴别力只有在 effect 二次运行时才体现。
  let current = options;

  function Probe(): React.ReactNode {
    hook = useSessionChangesPreview({
      activeSession: session(),
      activeSessionWorkspaceId: "ws-1",
      contentView: current.contentView ?? "default",
      activeChangeset: "activeChangeset" in current
        ? current.activeChangeset!
        : null,
      sessionChangesLoading: current.sessionChangesLoading ?? false,
      sessionChangesError: current.sessionChangesError ?? null,
      layoutAuxiliaryVisible: "layoutAuxiliaryVisible" in current
        ? current.layoutAuxiliaryVisible
        : undefined,
      auxiliaryVisible: current.auxiliaryVisible ?? false,
      auxiliaryTab: current.auxiliaryTab ?? "files",
      activeTurnTimeline: null,
      conversationCount: 1,
      activePreviewPath: current.activePreviewPath ?? null,
      loadSessionChangesets: async () => ({ items: [] } as never),
      switchContentView: (view) => switches.push(view),
      setAuxiliaryVisible: (visible) => auxiliaryVisibleWrites.push(visible),
      setStatus: (text) => statuses.push(text),
      openSessionChangePreview: (target, targetFile) =>
        openedPreviews.push([target.changeset_id, targetFile.file_path]),
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  return {
    mount: async () => {
      await act(async () => {
        renderer = create(<Probe />);
      });
      mountedRenderers.push(renderer!);
    },
    rerender: async (next: MountOptions) => {
      current = { ...current, ...next };
      await act(async () => {
        renderer!.update(<Probe />);
      });
    },
    hook: () => hook!,
    openedPreviews,
    switches,
    auxiliaryVisibleWrites,
    statuses,
  };
}

describe("会话文件变更展示编排", () => {
  test("默认视图且后端显式隐藏右侧栏时不强行展开", async () => {
    const mounted = mountHook({ layoutAuxiliaryVisible: false });
    await mounted.mount();
    expect(mounted.auxiliaryVisibleWrites).toEqual([]);
  });

  test("变更视图下未显式隐藏右侧栏时自动展开", async () => {
    const mounted = mountHook({ contentView: "changes" });
    await mounted.mount();
    expect(mounted.auxiliaryVisibleWrites).toEqual([true]);
  });

  test("变更视图下后端显式隐藏右侧栏时不自动展开", async () => {
    const mounted = mountHook({
      contentView: "changes",
      layoutAuxiliaryVisible: false,
    });
    await mounted.mount();
    expect(mounted.auxiliaryVisibleWrites).toEqual([]);
  });

  test("变更视图有文件时自动打开首个文件预览", async () => {
    const mounted = mountHook({
      contentView: "changes",
      activeChangeset: changeset([file("src/a.ts")]),
    });
    await mounted.mount();

    expect(mounted.openedPreviews).toEqual([["cs-1", "src/a.ts"]]);
  });

  test("同一文件在重渲染后不重复打开预览", async () => {
    const mounted = mountHook({
      contentView: "changes",
      activeChangeset: changeset([file("src/a.ts")]),
    });
    await mounted.mount();

    expect(mounted.openedPreviews).toEqual([["cs-1", "src/a.ts"]]);

    // 重渲染推进 changeset 身份，effect 再次运行；去重键命中，不能第二次打开。
    await mounted.rerender({
      activeChangeset: changeset([file("src/a.ts")]),
      activePreviewPath: "session-diff://cs-1/src%2Fa.ts",
    });
    expect(mounted.openedPreviews).toEqual([["cs-1", "src/a.ts"]]);
  });

  test("打开变更文件预览按 changeset 与文件名透传", async () => {
    const mounted = mountHook({
      activeChangeset: changeset([file("src/a.ts"), file("src/b.ts")]),
    });
    await mounted.mount();

    act(() => mounted.hook().openChangesetFileInPreview(file("src/b.ts")));
    expect(mounted.openedPreviews).toEqual([["cs-1", "src/b.ts"]]);
  });

  test("没有活动变更集时不打开任何预览", async () => {
    const mounted = mountHook();
    await mounted.mount();

    act(() => mounted.hook().openChangesetFileInPreview(file("src/a.ts")));
    expect(mounted.openedPreviews).toEqual([]);
  });
});
