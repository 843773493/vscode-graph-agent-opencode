import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type {
  CreateGatewayPortForwardRequest,
  GatewayPortForward,
  GatewayPortForwardList,
  GatewayWorkspace,
} from "../../types/backend";
import WarmConfirmProvider from "../WarmConfirmProvider";
import WorkspacePortForwardPanel, {
  type WorkspacePortForwardApi,
} from "./WorkspacePortForwardPanel";

const remoteWorkspace: GatewayWorkspace = {
  workspace_id: "gw_remote_project",
  name: "远程项目",
  root_path: "/workspace/project",
  backend_url: "http://127.0.0.1:8010",
  connection_kind: "remote_gateway",
  status: "ready",
  active: true,
  managed: true,
  removable: true,
  system_default: false,
  remote: {
    gateway_connection_id: "remote_dev",
    remote_workspace_id: "remote_project",
    gateway_id: "gateway_dev",
    name: "开发服务器",
    host: "dev.example",
    port: 22,
    username: "developer",
    remote_gateway_port: 8014,
  },
  services: {},
  checked_at: "2026-08-02T00:00:00Z",
};

function forward(overrides: Partial<GatewayPortForward> = {}): GatewayPortForward {
  return {
    forward_id: "pf_vite",
    workspace_id: remoteWorkspace.workspace_id,
    connection_id: "remote_dev",
    remote_host: "127.0.0.1",
    remote_port: 5173,
    local_host: "127.0.0.1",
    local_port: 41001,
    protocol: "http",
    label: "Vite",
    status: "active",
    error: null,
    local_url: "http://127.0.0.1:41001",
    ...overrides,
  };
}

function list(
  items: GatewayPortForward[],
): GatewayPortForwardList {
  return { items };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function renderPanel(
  api: WorkspacePortForwardApi,
  workspace: GatewayWorkspace = remoteWorkspace,
  confirmStop: (item: GatewayPortForward) => Promise<boolean> = async () => true,
) {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <WorkspacePortForwardPanel
          apiPort={8014}
          workspace={workspace}
          active
          api={api}
          confirmStop={confirmStop}
        />
      </WarmConfirmProvider>,
    );
  });
  return renderer;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function renderedText(renderer: ReactTestRenderer): string {
  return JSON.stringify(renderer.toJSON());
}

describe("工作区端口转发面板", () => {
  test("本地工作区空状态仍明确显示端口目录归属", () => {
    const api: WorkspacePortForwardApi = {
      list: async () => list([]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api, {
      ...remoteWorkspace,
      name: "本地项目",
      connection_kind: "local",
      remote: null,
    });

    expect(renderedText(renderer)).toContain("工作区端口");
    expect(renderedText(renderer)).toContain("当前是本地工作区");
    expect(renderedText(renderer)).toContain("无需 SSH 转发");
    renderer.unmount();
  });

  test("默认优先显示工作区端口列表，并可展开和取消新增表单", async () => {
    const api: WorkspacePortForwardApi = {
      list: async () => list([forward(), forward({ forward_id: "pf_terminal", remote_port: 8013 })]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();

    expect(renderer.root.findAllByType("form")).toHaveLength(0);
    expect(renderedText(renderer)).not.toContain("筛选端口...");
    expect(renderedText(renderer)).not.toContain("新增端口");
    expect(renderedText(renderer)).not.toContain("仅监听本机 127.0.0.1");

    act(() => renderer.root.findByProps({ role: "table" }).props.onContextMenu({ preventDefault() {} }));
    const addButton = renderer.root.findByProps({ "aria-controls": "workspace-port-forward-form" });
    act(() => addButton.props.onClick());
    expect(renderer.root.findAllByType("form")).toHaveLength(1);
    expect(renderedText(renderer)).toContain("远端端口");
    expect(renderer.root.findByProps({ className: "port-forward-create-cancel" })).toBeDefined();

    act(() => renderer.root.findByProps({ className: "port-forward-create-cancel" }).props.onClick());
    expect(renderer.root.findAllByType("form")).toHaveLength(0);
    renderer.unmount();
  });

  test("筛选框按远端或本地端口过滤列表", async () => {
    const api: WorkspacePortForwardApi = {
      list: async () => list([
        forward({ label: "前端预览", remote_port: 5173 }),
        forward({ forward_id: "pf_terminal", label: "终端服务", remote_port: 8013, local_port: 41002 }),
      ]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();

    act(() => renderer.root.findByProps({ role: "table" }).props.onContextMenu({ preventDefault() {} }));
    const filterMenuItem = renderer.root.findAllByProps({ role: "menuitem" }).find(
      (item) => item.children.includes("筛选端口"),
    );
    expect(filterMenuItem).toBeDefined();
    act(() => filterMenuItem!.props.onClick());
    const filter = renderer.root.findByProps({ placeholder: "筛选端口..." });
    expect(renderer.root.findAllByType("article")).toHaveLength(2);
    act(() => filter.props.onChange({ target: { value: "8013" } }));
    expect(renderer.root.findAllByType("article")).toHaveLength(1);
    expect(renderedText(renderer)).toContain("终端服务");
    expect(renderedText(renderer)).not.toContain("前端预览");
    renderer.unmount();
  });

  test("通过更多操作更改本地端口并替换完整列表", async () => {
    const payloads: number[] = [];
    const api: WorkspacePortForwardApi = {
      list: async () => list([forward()]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async (_port, _workspaceId, _forwardId, payload) => {
        payloads.push(payload.local_port);
        return list([forward({ local_port: payload.local_port, local_url: `http://127.0.0.1:${payload.local_port}` })]);
      },
    };
    const renderer = renderPanel(api);
    await flush();

    const menuItem = renderer.root.findAllByProps({ role: "menuitem" }).find(
      (item) => item.children.includes("更改本地端口"),
    );
    expect(menuItem).toBeDefined();
    const requiredMenuItem = menuItem!;
    act(() => requiredMenuItem.props.onClick({ currentTarget: { closest: () => ({ removeAttribute() {} }) } }));
    const editForm = renderer.root.findByProps({ className: "port-forward-edit-form" });
    const portInput = editForm.findByType("input");
    act(() => portInput.props.onChange({ target: { value: "41009" } }));
    act(() => editForm.props.onSubmit({ preventDefault() {} }));
    await flush();

    expect(payloads).toEqual([41009]);
    expect(renderedText(renderer)).toContain("127.0.0.1:41009");
    expect(renderedText(renderer)).not.toContain("127.0.0.1:41001");
    renderer.unmount();
  });

  test("展开表单默认使用 HTTP，并将本地端口留空交给后端分配", async () => {
    const api: WorkspacePortForwardApi = {
      list: async () => list([]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();
    act(() => renderer.root.findByProps({ "aria-controls": "workspace-port-forward-form" }).props.onClick());

    const protocol = renderer.root.findByType("select");
    const localPort = renderer.root.findByProps({ placeholder: "自动分配" });
    const remotePort = renderer.root.findByProps({ placeholder: "例如 5173" });
    expect(protocol.props.value).toBe("http");
    expect(localPort.props.value).toBe("");
    expect(remotePort.props.value).toBe("");
    expect(renderedText(renderer)).toContain("远端端口");
    expect(renderer.root.findByProps({ className: "port-forward-create" }).props.disabled).toBe(true);
    act(() => remotePort.props.onChange({ target: { value: "5173" } }));
    expect(renderer.root.findByProps({ className: "port-forward-create" }).props.disabled).toBe(false);
    renderer.unmount();
  });

  test("提交完整负载，创建期间即时禁用表单，成功后使用完整列表替换", async () => {
    const pending = deferred<GatewayPortForwardList>();
    const payloads: CreateGatewayPortForwardRequest[] = [];
    const api: WorkspacePortForwardApi = {
      list: async () => list([]),
      create: async (_port, _workspaceId, payload) => {
        payloads.push(payload);
        return pending.promise;
      },
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();
    act(() => renderer.root.findByProps({ "aria-controls": "workspace-port-forward-form" }).props.onClick());

    act(() => {
      renderer.root.findByProps({ placeholder: "例如 5173" }).props.onChange({
        target: { value: "5173" },
      });
      renderer.root.findByProps({ placeholder: "自动分配" }).props.onChange({
        target: { value: "4173" },
      });
      renderer.root.findByType("select").props.onChange({ target: { value: "https" } });
      renderer.root.findByProps({ placeholder: "例如 Vite 开发服务器" }).props.onChange({
        target: { value: "前端预览" },
      });
    });
    act(() => {
      renderer.root.findByType("form").props.onSubmit({ preventDefault() {} });
    });

    expect(payloads).toEqual([{
      remote_port: 5173,
      local_port: 4173,
      protocol: "https",
      label: "前端预览",
    }]);
    expect(renderedText(renderer)).toContain("正在创建…");

    await act(async () => pending.resolve(list([forward({
      label: "前端预览",
      local_port: 4173,
      protocol: "https",
      local_url: "https://127.0.0.1:4173",
    })])));
    expect(renderedText(renderer)).toContain("前端预览");
    expect(renderedText(renderer)).toContain("127.0.0.1:4173");
    expect(renderer.root.findAllByType("form")).toHaveLength(0);
    act(() => renderer.root.findByProps({ role: "table" }).props.onContextMenu({ preventDefault() {} }));
    expect(renderedText(renderer)).toContain("新增端口");
    renderer.unmount();
  });

  test("创建失败后重新读取后端权威列表并保留失败反馈", async () => {
    let listCalls = 0;
    const api: WorkspacePortForwardApi = {
      list: async () => {
        listCalls += 1;
        return listCalls === 1 ? list([]) : list([forward({ label: "后端现状" })]);
      },
      create: async () => {
        throw new Error("本地端口已占用");
      },
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();
    act(() => renderer.root.findByProps({ "aria-controls": "workspace-port-forward-form" }).props.onClick());
    act(() => {
      renderer.root.findByProps({ placeholder: "例如 5173" }).props.onChange({
        target: { value: "5173" },
      });
    });
    act(() => {
      renderer.root.findByType("form").props.onSubmit({ preventDefault() {} });
    });
    await flush();

    expect(listCalls).toBe(2);
    expect(renderedText(renderer)).toContain("创建端口转发失败");
    expect(renderedText(renderer)).toContain("本地端口已占用");
    expect(renderedText(renderer)).toContain("后端现状");
    expect(renderer.root.findAllByType("form")).toHaveLength(1);
    renderer.unmount();
  });

  test("停止前要求确认，停止失败后刷新并给出明确反馈", async () => {
    let listCalls = 0;
    let confirmations = 0;
    const api: WorkspacePortForwardApi = {
      list: async () => {
        listCalls += 1;
        return list([forward()]);
      },
      create: async () => list([]),
      remove: async () => {
        throw new Error("SSH 进程未退出");
      },
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api, remoteWorkspace, async () => {
      confirmations += 1;
      return true;
    });
    await flush();
    const stopButton = renderer.root.findAllByType("button").find(
      (button) => button.children.includes("停止转发"),
    );
    expect(stopButton).toBeDefined();
    act(() => stopButton!.props.onClick());
    await flush();

    expect(confirmations).toBe(1);
    expect(listCalls).toBe(2);
    expect(renderedText(renderer)).toContain("停止端口转发失败");
    expect(renderedText(renderer)).toContain("SSH 进程未退出");
    renderer.unmount();
  });

  test("错误条目可以重新连接，并用重连响应替换列表", async () => {
    const reconnected = forward({ status: "active", error: null });
    let reconnectId = "";
    const api: WorkspacePortForwardApi = {
      list: async () => list([forward({ status: "error", error: "网络已断开" })]),
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async (_port, _workspaceId, forwardId) => {
        reconnectId = forwardId;
        return list([reconnected]);
      },
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();
    const reconnectButton = renderer.root.findAllByType("button").find(
      (button) => button.children.includes("重新连接"),
    );
    expect(reconnectButton).toBeDefined();
    act(() => reconnectButton!.props.onClick());
    await flush();

    expect(reconnectId).toBe("pf_vite");
    expect(renderedText(renderer)).toContain("已转发");
    expect(renderedText(renderer)).not.toContain("网络已断开");
    renderer.unmount();
  });

  test("切换工作区后丢弃旧列表并读取新工作区", async () => {
    const requestedWorkspaceIds: string[] = [];
    const api: WorkspacePortForwardApi = {
      list: async (_port, workspaceId) => {
        requestedWorkspaceIds.push(workspaceId);
        return list([
          forward({
            forward_id: `pf_${workspaceId}`,
            workspace_id: workspaceId,
            label: workspaceId,
          }),
        ]);
      },
      create: async () => list([]),
      remove: async () => list([]),
      reconnect: async () => list([]),
      changeLocalPort: async () => list([]),
    };
    const renderer = renderPanel(api);
    await flush();
    const secondWorkspace: GatewayWorkspace = {
      ...remoteWorkspace,
      workspace_id: "gw_remote_api",
      name: "远程 API",
      root_path: "/workspace/api",
    };
    act(() => {
      renderer.update(
        <WarmConfirmProvider>
          <WorkspacePortForwardPanel
            apiPort={8014}
            workspace={secondWorkspace}
            active
            api={api}
            confirmStop={async () => true}
          />
        </WarmConfirmProvider>,
      );
    });
    await flush();

    expect(requestedWorkspaceIds).toEqual(["gw_remote_project", "gw_remote_api"]);
    expect(renderedText(renderer)).toContain("gw_remote_api");
    expect(renderedText(renderer)).not.toContain("pf_gw_remote_project");
    renderer.unmount();
  });
});
