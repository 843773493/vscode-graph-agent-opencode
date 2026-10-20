import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import * as ReactDOM from "react-dom";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "../../gatewayApi";
import * as sessionApi from "../../api/session/sessions";
import { HttpRequestError } from "../../api/http";
import type { GatewayRuntimeGroupRequest, GatewayWorkspace, SessionForkRequest, SessionForkResult, SessionForkSourcePoint } from "../../types/backend";
import SessionContextForkDialog from "../../components/overlays/SessionContextForkDialog";
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
  confirmResults?: boolean[];
  removalFailure?: Error | ((force: boolean) => Error | undefined);
  deleteSession?: Parameters<typeof useSessionCatalogActions>[0]["deleteSession"];
  forkSessionContext?: Parameters<typeof useSessionCatalogActions>[0]["forkSessionContext"];
  onForkCreated?: Parameters<typeof useSessionCatalogActions>[0]["onForkCreated"];
  renderForkDialog?: boolean;
}

const mountedRenderers: ReactTestRenderer[] = [];
const spies: Array<{ mockRestore: () => void }> = [];

afterEach(() => {
  for (const spy of spies.splice(0)) spy.mockRestore();
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
  const removalRequests: Array<[string, GatewayRuntimeGroupRequest | undefined, boolean | undefined]> = [];
  const confirmations: Array<{ message: string; confirmText: string }> = [];
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
      confirm: async (input) => {
        confirmations.push(input);
        return options.confirmResults?.[confirmations.length - 1] ?? options.confirmResult ?? true;
      },
      setStatus: (text) => statuses.push(text),
      activateGatewayWorkspace: async (workspaceId) => {
        activated.push(workspaceId);
      },
      createSession: async (title, workspaceId, folderId) => {
        created.push([title, workspaceId, folderId]);
        return {} as never;
      },
      openWorkspaceSession: async () => {},
      removeGatewayWorkspace: async (workspaceId, request, force) => {
        removed.push(workspaceId);
        removalRequests.push([workspaceId, request, force]);
        const failure = typeof options.removalFailure === "function"
          ? options.removalFailure(force ?? false)
          : !force ? options.removalFailure : undefined;
        if (failure) throw failure;
      },
      deleteSession: async (sessionId, workspaceId) => {
        deleted.push([sessionId, workspaceId]);
        await options.deleteSession?.(sessionId, workspaceId);
      },
      renameSession: async (sessionId, title, workspaceId) => {
        renamed.push([sessionId, title, workspaceId]);
        if (renameShouldFail) {
          throw new Error("重命名被后端拒绝");
        }
      },
      forkSessionContext: options.forkSessionContext ?? (async () => { throw new Error("此目录测试未装配 fork"); }),
      onForkCreated: options.onForkCreated,
      readSessionSelectionIntent: () => 0,
      setSessionParent: async () => {},
    });
    return options.renderForkDialog ? <SessionContextForkDialog state={hook.forkDialog}
      onCancel={hook.closeForkDialog} onSubmit={hook.submitForkDialog} onSelectionChange={hook.updateForkDialogSelection} /> : null;
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
    renderer: () => renderer!,
    created,
    activated,
    removed,
    removalRequests,
    confirmations,
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

  test("owner 删除复合失败保留原反馈，提交前快照失败仍由 caller 显示", async () => {
    const managed = workspace({
      workspace_id: "ws-managed",
      managed: true,
      connection_kind: "local",
    });
    const repairFailure = new Error("删除已提交但目录排空失败");
    const refreshFailure = new Error("新活动工作区刷新失败");
    const ownerFailure = new AggregateError(
      [repairFailure, refreshFailure],
      `${repairFailure.message}；${refreshFailure.message}`,
    );
    const precommitFailure = new Error("读取工作区快照失败");
    const snapshot = spyOn(gatewayApi, "listGatewayWorkspaces")
      .mockResolvedValueOnce({ active_workspace_id: "ws-active", items: [managed] })
      .mockRejectedValueOnce(precommitFailure);
    spies.push(snapshot);
    const ownerFailureMount = mountHook({
      gatewayWorkspaces: [managed],
      removalFailure: ownerFailure,
    });
    await ownerFailureMount.mount();

    await act(async () => {
      ownerFailureMount.hook().removeWorkspace("ws-managed", "工作区");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(ownerFailureMount.removed).toEqual(["ws-managed"]);
    expect(ownerFailureMount.statuses).toEqual([]);

    const precommitMount = mountHook({ gatewayWorkspaces: [managed] });
    await precommitMount.mount();

    await act(async () => {
      precommitMount.hook().removeWorkspace("ws-managed", "工作区");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(precommitMount.confirmations).toEqual([]);
    expect(precommitMount.removed).toEqual([]);
    expect(precommitMount.statuses).toEqual([
      `删除工作区失败: ${precommitFailure.message}`,
    ]);
  });

  test("删除会话确认后本地收敛，不再叠加强制刷新目录", async () => {
    const mounted = mountHook();
    await mounted.mount();

    await act(async () => {
      mounted.hook().removeSession("ses-1", "标题", "ws-1");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mounted.deleted).toEqual([["ses-1", "ws-1"]]);
    // §10.3 前端半边：删除成功路径 MUST NOT 无条件重拉会话列表或强制刷新目录树；
    // 会话列表已在 deleteSession 内本地收敛，目录树由会话集合派生的 sync key 驱动。
    expect(mounted.hook().sessionCatalogRefreshVersions.get("ws-1")).toBeUndefined();
  });

  test("删除会话外层提示保留排空修复与分支刷新两项失败", async () => {
    const repair = new Error("会话目录操作 op_repair 删除已提交但排空失败，需修复: 日期目录隔离失败");
    const refresh = new Error("父分支读取失败");
    const failure = new AggregateError([repair, refresh], `${repair.message}；目录分支刷新失败: ${refresh.message}`);
    const mounted = mountHook({ deleteSession: async () => { throw failure; } });
    await mounted.mount();
    await act(async () => {
      mounted.hook().removeSession("ses-1", "标题", "ws-1");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mounted.deleted).toEqual([["ses-1", "ws-1"]]);
    expect(mounted.statuses[mounted.statuses.length - 1]).toBe(`删除会话失败: ${failure.message}`);
    expect(mounted.hook().sessionCatalogRefreshVersions.get("ws-1")).toBeUndefined();
  });
});

describe("上下文分支候选与冻结重试", () => {
  const point = (overrides: Partial<SessionForkSourcePoint> = {}): SessionForkSourcePoint => ({
    source_uri: "boxteam://workspace/ws-source/resources/session/ses-source/threads/tid-source",
    source_view_id: "view-source", source_view_revision: 9, anchor_item_id: "item-source",
    child_container_uri: "boxteam://workspace/ws-source/resources/session/ses-source",
    main_container_uri: "boxteam://workspace/ws-source/resources/session",
    title: "目标预填名称", goal_available: false, resources: [], live_resources: [],
    resource_mode: "view_prefix_fork", resource_anchor_mode: "inclusive", ...overrides,
  });

  test("首次消息点击使用展示快照与来源 owner，409 不改取活动 head", async () => {
    const read = spyOn(sessionApi, "getSessionForkSourcePoint")
      .mockRejectedValue(new HttpRequestError(409, "source-mismatch: 展示快照已失效",
        { reason: "source-mismatch" }, "/api/v1/sessions/ses-source/fork-source-point"));
    spies.push(read);
    const mounted = mountHook({ activeSessionId: "ses-other", activeGatewayWorkspaceId: "ws-other" });
    await mounted.mount();
    const selection = { threadId: "tid-source", turnId: "turn-source", sourceViewId: "view-display", sourceViewRevision: 8 };
    await act(async () => { await mounted.hook().forkSession("ws-source", "ses-source", selection); });
    expect(read.mock.calls).toEqual([[8014, "ses-source", selection, "ws-source"]]);
    expect(mounted.hook().forkDialog!.point).toBeNull();
    expect(mounted.hook().forkDialog!.error).toContain("展示快照已失效");
    expect(mounted.hook().forkDialog!.sourceThreadId).toBe("tid-source");
  });

  test("范围与目标编辑拒绝迟到候选，读取期间禁止提交，失败后重试首次请求", async () => {
    let finishOld!: (value: SessionForkSourcePoint) => void;
    let finishCurrent!: (value: SessionForkSourcePoint) => void;
    const oldRead = new Promise<SessionForkSourcePoint>((resolve) => { finishOld = resolve; });
    const currentRead = new Promise<SessionForkSourcePoint>((resolve) => { finishCurrent = resolve; });
    const read = spyOn(sessionApi, "getSessionForkSourcePoint")
      .mockResolvedValueOnce(point())
      .mockImplementationOnce(() => oldRead)
      .mockImplementationOnce(() => currentRead);
    spies.push(read);
    const submissions: SessionForkRequest[] = [];
    const mounted = mountHook({ forkSessionContext: async (_workspace, _source, request) => {
      submissions.push(request);
      throw new Error("目标暂时不可达");
    } });
    await mounted.mount();
    await act(async () => { await mounted.hook().forkSession("ws-route", "ses-source", {
      threadId: "tid-source", itemId: "item-source", sourceViewId: "view-source", sourceViewRevision: 9,
    }); });
    let pendingOld!: Promise<void>;
    let pendingCurrent!: Promise<void>;
    await act(async () => { pendingOld = mounted.hook().updateForkDialogSelection({ mode: "full_rollout_copy" }); });
    await act(async () => { pendingCurrent = mounted.hook().updateForkDialogSelection({ targetContainerUri: "boxteam://gateway-c/workspace/ws-target/resources/session" }); });
    const request: SessionForkRequest = {
      idempotency_key: mounted.hook().forkDialog!.idempotencyKey,
      source_uri: point().source_uri, source_view_id: "view-source", source_view_revision: 9,
      anchor_item_id: null, mode: "full_rollout_copy", anchor_mode: "inclusive",
      target_container_uri: mounted.hook().forkDialog!.targetContainerUri,
      creation_kind: "new_session_main", relationship: "detached", resources: [],
    };
    await act(async () => { await mounted.hook().submitForkDialog(request); });
    expect(submissions).toEqual([]);
    const current = point({ anchor_item_id: null, resource_mode: "full_rollout_copy", resources: [{
      resource_uri: "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_526468c69b637b128bacbc68e83b284b/attachments/att_b7ce3abba40fd6a19282a0da38ea7c9d", name: "当前附件",
      content_type: "text/plain", availability: "available", required_for_context: false,
    }] });
    await act(async () => { finishCurrent(current); await pendingCurrent; });
    await act(async () => { finishOld(point({ anchor_item_id: null, resource_mode: "full_rollout_copy" })); await pendingOld; });
    expect(mounted.hook().forkDialog!.point).toBe(current);
    expect(mounted.hook().forkDialog!.targetContainerUri).toBe(request.target_container_uri);
    expect(read.mock.calls[2]![2]).toEqual({
      threadId: "tid-source", itemId: undefined, sourceViewId: "view-source", sourceViewRevision: 9,
      mode: "full_rollout_copy", anchorMode: "inclusive",
    });
    await act(async () => { await mounted.hook().submitForkDialog(request); });
    expect(mounted.hook().forkDialog!.error).toBe("目标暂时不可达");
    await act(async () => { await mounted.hook().submitForkDialog({ ...request, title: "迟到编辑" }); });
    expect(submissions).toEqual([request, request]);
    expect(submissions[0]).toBe(submissions[1]);
  });

  test("完整历史无需截止消息，切回前缀仍使用首次点击点", async () => {
    const read = spyOn(sessionApi, "getSessionForkSourcePoint")
      .mockResolvedValueOnce(point())
      .mockResolvedValueOnce(point({ anchor_item_id: null, resource_mode: "full_rollout_copy" }))
      .mockResolvedValueOnce(point({ anchor_item_id: "item-completed", resource_mode: "context_fork" }))
      .mockResolvedValueOnce(point());
    spies.push(read);
    const mounted = mountHook();
    await mounted.mount();
    await act(async () => { await mounted.hook().forkSession("ws-route", "ses-source", {
      itemId: "item-source", sourceViewId: "view-source", sourceViewRevision: 9,
    }); });
    await act(async () => { await mounted.hook().updateForkDialogSelection({ mode: "full_rollout_copy" }); });
    expect(mounted.hook().forkDialog!.point!.anchor_item_id).toBeNull();
    await act(async () => { await mounted.hook().updateForkDialogSelection({ mode: "context_fork" }); });
    expect(mounted.hook().forkDialog!.point!.anchor_item_id).toBe("item-completed");
    await act(async () => { await mounted.hook().updateForkDialogSelection({ mode: "view_prefix_fork" }); });
    expect(mounted.hook().forkDialog!.error).toBeNull();
    expect(read.mock.calls[3]![2]).toEqual({
      threadId: undefined, itemId: "item-source", sourceViewId: "view-source", sourceViewRevision: 9,
      mode: "view_prefix_fork", anchorMode: "inclusive",
    });
  });

  test("无默认锚点时显示冻结来源且拒绝前缀提交，允许显式完整历史", async () => {
    const read = spyOn(sessionApi, "getSessionForkSourcePoint")
      .mockResolvedValueOnce(point({ anchor_item_id: null }))
      .mockResolvedValueOnce(point({ anchor_item_id: null, resource_mode: "full_rollout_copy" }));
    spies.push(read);
    const submitted: SessionForkRequest[] = [];
    const mounted = mountHook({ forkSessionContext: async (_workspace, _source, request) => {
      submitted.push(request);
      throw new Error("保留原请求");
    } });
    await mounted.mount();
    await act(async () => { await mounted.hook().forkSession("ws-route", "ses-source"); });
    const request: SessionForkRequest = {
      idempotency_key: mounted.hook().forkDialog!.idempotencyKey, source_uri: point().source_uri,
      source_view_id: "view-source", source_view_revision: 9, anchor_item_id: null,
      mode: "view_prefix_fork", anchor_mode: "inclusive", target_container_uri: point().child_container_uri,
      creation_kind: "new_child_thread", relationship: "detached", resources: [],
    };
    await act(async () => { await mounted.hook().submitForkDialog(request); });
    expect(submitted).toEqual([]);
    expect(mounted.hook().forkDialog!.mode).toBe("view_prefix_fork");
    await act(async () => { await mounted.hook().updateForkDialogSelection({ mode: "full_rollout_copy" }); });
    expect(mounted.hook().forkDialog!.error).toBeNull();
    await act(async () => { await mounted.hook().submitForkDialog({ ...request, mode: "full_rollout_copy" }); });
    expect(submitted).toEqual([{ ...request, mode: "full_rollout_copy" }]);
  });

  test("真实分支对话框默认不关联，显选发送typed身份，部分失败打开目标并冻结原请求", async () => {
    const priorWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
    const priorDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
    Object.defineProperty(globalThis, "window", { configurable: true, value: { addEventListener() {}, removeEventListener() {} } });
    Object.defineProperty(globalThis, "document", { configurable: true, value: { body: {} } });
    // renderer 只替换 Portal 容器，选择、提交及错误展示仍由真实组件和 hook 执行。
    const portal = spyOn(ReactDOM, "createPortal").mockImplementation((children) => children as React.ReactPortal);
    spies.push(portal);
    const candidate = { resource_id: "term-live", workspace_id: "ws-source", kind: "terminal", retention_scope: "workspace",
      source_thread: { session_id: "ses-source", thread_id: "tid-source" }, thread_associations: [{ session_id: "ses-source", thread_id: "tid-source" }],
      owner_revision: 7, status: "running", reconciliation_status: "verified", available_actions: ["attach"], metadata: {},
      created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z" };
    const read = spyOn(sessionApi, "getSessionForkSourcePoint").mockResolvedValue(point({ live_resources: [candidate] }));
    spies.push(read);
    const submitted: SessionForkRequest[] = [];
    const opened: SessionForkResult[] = [];
    const result: SessionForkResult = {
      session: { session_id: "ses-source", workspace_id: "ws-source", resource_identity: "session:ws-source:ses-source",
        uri: point().child_container_uri, scope: "workspace", title: "来源", current_agent_id: "default", created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z", title_source: "default", kind: "normal" },
      thread_id: "tid-published", thread_uri: "boxteam://workspace/ws-source/resources/session/ses-source/threads/tid-published",
      creation_kind: "new_child_thread", source_uri: point().source_uri, source_view_id: "view-source", source_view_revision: 9,
      anchor_item_id: "item-source", mode: "view_prefix_fork", goal_id: null, resources: [{
        owner_reference: { kind: "terminal", resource_id: "term-live", workspace_id: "ws-source", expected_revision: 7 },
        owner_resource: candidate, action: "reuse_reference", availability: "unavailable", target_uri: null,
        operation_state: "dispatching", reason: "关联回执待核实",
      }],
    };
    const mounted = mountHook({ renderForkDialog: true, forkSessionContext: async (_workspace, _source, request) => {
      submitted.push(request);
      if (request.resources.length === 0) throw new Error("保留默认请求供检查");
      return result;
    }, onForkCreated: async (_workspace, created) => { opened.push(created); } });
    try {
      await mounted.mount();
      await act(async () => { await mounted.hook().forkSession("ws-source", "ses-source"); });
      const checkbox = () => mounted.renderer().root.findByProps({ "aria-label": "关联终端 term-live" });
      expect(checkbox().props.checked).toBe(false);
      await act(async () => { mounted.renderer().root.findByType("form").props.onSubmit({ preventDefault() {} }); });
      expect(submitted[0]!.resources).toEqual([]);
      await act(async () => { mounted.hook().closeForkDialog(); });
      await act(async () => { await mounted.hook().forkSession("ws-source", "ses-source"); });
      await act(async () => { checkbox().props.onChange({ target: { checked: true } }); });
      await act(async () => { mounted.renderer().root.findByType("form").props.onSubmit({ preventDefault() {} }); });
      const selected = submitted[1]!;
      expect(selected.resources).toEqual([{ action: "reuse_reference", owner_reference: {
        kind: "terminal", resource_id: "term-live", workspace_id: "ws-source", expected_revision: 7,
      } }]);
      expect(opened).toEqual([result]);
      expect(mounted.hook().forkDialog!.request).toBe(selected);
      expect(mounted.renderer().root.findByProps({ role: "alert" }).children.join("")).toContain("目标已创建（tid-published），运行资源关联失败或待核实：term-live：关联回执待核实");
      await act(async () => { mounted.renderer().root.findByType("form").props.onSubmit({ preventDefault() {} }); });
      expect(submitted[2]).toBe(selected);
      expect(mounted.hook().forkDialog!.idempotencyKey).toBe(selected.idempotency_key);
    } finally {
      act(() => mounted.renderer().unmount());
      mountedRenderers.splice(mountedRenderers.indexOf(mounted.renderer()), 1);
      portal.mockRestore();
      if (priorWindow) Object.defineProperty(globalThis, "window", priorWindow); else Reflect.deleteProperty(globalThis, "window");
      if (priorDocument) Object.defineProperty(globalThis, "document", priorDocument); else Reflect.deleteProperty(globalThis, "document");
    }
  });
});


describe("托管工作区删除的完整组确认", () => {
  const members = [
    workspace({ workspace_id: "ws-a", name: "A", managed: true, connection_kind: "local", backend_generation: "generation-1", affected_workspace_ids: ["ws-a", "ws-b"] }),
    workspace({ workspace_id: "ws-b", name: "B", managed: true, connection_kind: "local", backend_generation: "generation-1", affected_workspace_ids: ["ws-a", "ws-b"] }),
  ];
  function snapshot() {
    const spy = spyOn(gatewayApi, "listGatewayWorkspaces").mockResolvedValue({ active_workspace_id: "ws-b", items: members });
    spies.push(spy);
    return spy;
  }
  const blocked = () => new HttpRequestError(409, "删除被阻塞", {
    workspace_id: "ws-a", status: "blocked", backend_generation: "generation-1", affected_workspace_ids: ["ws-a", "ws-b"],
    blockers: [{ workspace_id: "ws-b", kind: "job", resource_id: "job-b", detail: "Job 正在执行" }],
  }, "/api/gateway/workspaces/ws-a");
  test("完整确认后 safe 请求移除发起成员，并保留其余成员", async () => {
    snapshot();
    const mounted = mountHook({ gatewayWorkspaces: members });
    await mounted.mount();
    await act(async () => { mounted.hook().removeWorkspace("ws-a", "A"); });
    expect(mounted.removalRequests).toEqual([["ws-a", {
      expected_backend_generation: "generation-1", expected_workspace_ids: ["ws-a", "ws-b"], desired_workspace_ids: ["ws-b"],
    }, undefined]]);
    expect(mounted.confirmations[0]?.message).toContain("ws-b");
  });
  test("safe 阻塞后单独确认 force，并原样保留已确认合同", async () => {
    const listed = snapshot();
    const mounted = mountHook({ gatewayWorkspaces: members, removalFailure: blocked(), confirmResults: [true, true] });
    await mounted.mount();
    await act(async () => { mounted.hook().removeWorkspace("ws-a", "A"); });
    expect(listed).toHaveBeenCalledTimes(1);
    expect(mounted.removalRequests.length).toBe(2);
    expect(mounted.removalRequests[1]?.[1]).toBe(mounted.removalRequests[0]?.[1]);
    expect(mounted.removalRequests[1]?.[2]).toBe(true);
    expect(mounted.confirmations[1]?.message).toContain("Job 正在执行");
    expect(mounted.confirmations[1]?.message).toContain("interrupted");
  });
  test("force 删除失败时由 owner 保持错误归属，caller 不二写通用失败状态", async () => {
    snapshot();
    const forceFailure = new Error("强制删除后活动工作区加载失败");
    const mounted = mountHook({
      gatewayWorkspaces: members,
      removalFailure: (force) => force ? forceFailure : blocked(),
      confirmResults: [true, true],
    });
    await mounted.mount();
    await act(async () => { mounted.hook().removeWorkspace("ws-a", "A"); });
    expect(mounted.removalRequests.map(([, , force]) => force)).toEqual([undefined, true]);
    expect(mounted.statuses).toEqual([]);
  });
  test("阻塞回执重复成员时拒绝 force 确认", async () => {
    snapshot();
    const duplicateMemberFailure = new HttpRequestError(409, "删除被阻塞", {
      workspace_id: "ws-a",
      status: "blocked",
      backend_generation: "generation-1",
      affected_workspace_ids: ["ws-a", "ws-a"],
      blockers: [{ workspace_id: "ws-b", kind: "job", resource_id: "job-b", detail: "Job 正在执行" }],
    }, "/api/gateway/workspaces/ws-a");
    const mounted = mountHook({
      gatewayWorkspaces: members,
      removalFailure: duplicateMemberFailure,
      confirmResults: [true, true],
    });
    await mounted.mount();

    await act(async () => { mounted.hook().removeWorkspace("ws-a", "A"); });

    expect(mounted.removalRequests).toHaveLength(1);
    expect(mounted.removalRequests[0]?.[2]).toBeUndefined();
    expect(mounted.confirmations).toHaveLength(1);
    expect(mounted.statuses).toEqual([
      "删除工作区失败: 删除阻塞回执与已确认的 Backend 完整合同不匹配",
    ]);
  });
  test("取消 force 保留阻塞删除结果，陈旧409不触发 force", async () => {
    snapshot();
    const declined = mountHook({ gatewayWorkspaces: members, removalFailure: blocked(), confirmResults: [true, false] });
    await declined.mount();
    await act(async () => { declined.hook().removeWorkspace("ws-a", "A"); });
    expect(declined.removalRequests.length).toBe(1);
    const stale = mountHook({ gatewayWorkspaces: members, removalFailure: new HttpRequestError(409, "generation 已陈旧", "generation 已陈旧", "/api/gateway/workspaces/ws-a") });
    await stale.mount();
    await act(async () => { stale.hook().removeWorkspace("ws-a", "A"); });
    expect(stale.removalRequests.length).toBe(1);
    expect(stale.confirmations.length).toBe(1);
    expect(stale.statuses).toEqual([]);
  });
});
