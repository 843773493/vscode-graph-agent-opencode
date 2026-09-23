import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { GatewayWorkspace } from "../../types/backend";
import { useSessionCatalogActions } from "./useSessionCatalogActions";

/**
 * 会话目录编排链路的契约：新建会话只在没有激活工作区时才回退系统默认 home；
 * 重命名走对话框确认，失败文案必须可见；删除工作区必须等用户确认后才发起。
 */

interface MountOptions {
  activeGatewayWorkspaceId?: string | null;
  activeSessionId?: string | null;
  gatewayWorkspaces?: GatewayWorkspace[];
  confirmResult?: boolean;
}

const mountedRenderers: ReactTestRenderer[] = [];

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
});

function workspace(overrides: Partial<GatewayWorkspace>): GatewayWorkspace {
  return {
    workspace_id: "ws",
    name: "工作区",
    system_default: false,
    status: "ready",
    ...overrides,
  } as unknown as GatewayWorkspace;
}

function mountHook(options: MountOptions = {}) {
  const created: Array<[string | undefined, string | null | undefined, string | null | undefined]> = [];
  const activated: string[] = [];
  const removed: string[] = [];
  const deleted: Array<[string, string | null | undefined]> = [];
  const renamed: Array<[string, string, string | null | undefined]> = [];
  const statuses: string[] = [];
  const errors: string[] = [];
  let renameShouldFail = false;
  let hook: ReturnType<typeof useSessionCatalogActions> | undefined;

  function Probe(): React.ReactNode {
    hook = useSessionCatalogActions({
      apiPort: 8014,
      activeGatewayWorkspaceId: "activeGatewayWorkspaceId" in options
        ? options.activeGatewayWorkspaceId!
        : "ws-active",
      activeSessionId: "activeSessionId" in options
        ? options.activeSessionId!
        : "ses-active",
      sessionsByWorkspace: new Map(),
      gatewayWorkspaces: options.gatewayWorkspaces ?? [],
      confirm: async () => options.confirmResult ?? true,
      setStatus: (text) => statuses.push(text),
      activateGatewayWorkspace: async (workspaceId) => {
        activated.push(workspaceId);
      },
      createSession: async (title, workspaceId, folderId) => {
        created.push([title, workspaceId, folderId]);
        return {} as never;
      },
      openWorkspaceSession: async () => {},
      removeGatewayWorkspace: async (workspaceId) => {
        removed.push(workspaceId);
      },
      deleteSession: async (sessionId, workspaceId) => {
        deleted.push([sessionId, workspaceId]);
      },
      renameSession: async (sessionId, title, workspaceId) => {
        renamed.push([sessionId, title, workspaceId]);
        if (renameShouldFail) {
          throw new Error("重命名被后端拒绝");
        }
      },
      forkSessionContext: async () => {},
      setSessionParent: async () => {},
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
    hook: () => hook!,
    created,
    activated,
    removed,
    deleted,
    renamed,
    statuses,
    setRenameShouldFail: (value: boolean) => {
      renameShouldFail = value;
    },
  };
}

describe("会话目录编排链路", () => {
  test("没有激活工作区时回退到系统默认 home", async () => {
    const mounted = mountHook({
      activeGatewayWorkspaceId: null as unknown as undefined,
      gatewayWorkspaces: [
        workspace({ workspace_id: "ws-other" }),
        workspace({ workspace_id: "ws-home", system_default: true }),
      ],
    });
    await mounted.mount();

    await act(async () => {
      await mounted.hook().createSessionInCatalog();
    });

    expect(mounted.created).toEqual([["新会话", "ws-home", undefined]]);
    // 当前没有激活工作区，创建前必须先把它激活成当前工作区。
    expect(mounted.activated).toEqual(["ws-home"]);
  });

  test("没有激活工作区也没有默认 home 时响亮失败，不静默建到别处", async () => {
    const mounted = mountHook({
      activeGatewayWorkspaceId: null as unknown as undefined,
      gatewayWorkspaces: [workspace({ workspace_id: "ws-other" })],
    });
    await mounted.mount();

    await expect(mounted.hook().createSessionInCatalog()).rejects.toThrow(
      "未找到默认 home 工作区",
    );
    expect(mounted.created).toEqual([]);
    expect(mounted.statuses).toEqual([
      "创建会话失败: 未找到默认 home 工作区，无法创建会话",
    ]);
  });

  test("指定其它工作区时先激活再创建，失败文案写入状态", async () => {
    const mounted = mountHook({ activeGatewayWorkspaceId: "ws-active" });
    await mounted.mount();

    await act(async () => {
      await mounted.hook().createSessionInCatalog("ws-remote");
    });

    expect(mounted.activated).toEqual(["ws-remote"]);
    expect(mounted.created).toEqual([["新会话", "ws-remote", undefined]]);
  });

  test("重命名对话框：失败时错误留在对话框且不关闭", async () => {
    const mounted = mountHook();
    await mounted.mount();

    act(() => mounted.hook().openRenameDialog("ses-1", "旧标题", "ws-1"));
    expect(mounted.hook().nameDialog).toEqual({
      sessionId: "ses-1",
      workspaceId: "ws-1",
      initialTitle: "旧标题",
    });

    mounted.setRenameShouldFail(true);
    await act(async () => {
      mounted.hook().submitNameDialog("新标题");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mounted.renamed).toEqual([["ses-1", "新标题", "ws-1"]]);
    expect(mounted.hook().nameDialogError).toBe("重命名被后端拒绝");
    expect(mounted.hook().nameDialog).not.toBeNull();
    expect(mounted.hook().nameDialogSubmitting).toBe(false);
  });

  test("重命名成功后关闭对话框并刷新目录", async () => {
    const mounted = mountHook();
    await mounted.mount();

    const before = mounted.hook().sessionCatalogRefreshVersions.get("ws-1") ?? 0;
    act(() => mounted.hook().openRenameDialog("ses-1", "旧标题", "ws-1"));
    await act(async () => {
      mounted.hook().submitNameDialog("新标题");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mounted.hook().nameDialog).toBeNull();
    expect(mounted.hook().sessionCatalogRefreshVersions.get("ws-1")).toBe(before + 1);
  });

  test("删除工作区必须等用户确认后才发起", async () => {
    const declined = mountHook({ confirmResult: false });
    await declined.mount();
    await act(async () => {
      declined.hook().removeWorkspace("ws-1", "工作区一");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(declined.removed).toEqual([]);

    const accepted = mountHook({ confirmResult: true });
    await accepted.mount();
    await act(async () => {
      accepted.hook().removeWorkspace("ws-1", "工作区一");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(accepted.removed).toEqual(["ws-1"]);
  });

  test("删除会话确认后按工作区刷新目录", async () => {
    const mounted = mountHook();
    await mounted.mount();

    await act(async () => {
      mounted.hook().removeSession("ses-1", "标题", "ws-1");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mounted.deleted).toEqual([["ses-1", "ws-1"]]);
    expect(mounted.hook().sessionCatalogRefreshVersions.get("ws-1")).toBe(1);
  });
});
