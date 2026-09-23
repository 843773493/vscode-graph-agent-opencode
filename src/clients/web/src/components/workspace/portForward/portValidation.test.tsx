import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type {
  GatewayPortForward,
  GatewayPortForwardList,
  GatewayWorkspace,
} from "../../../types/backend";
import WarmConfirmProvider from "../../shell/WarmConfirmProvider";
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

function forward(): GatewayPortForward {
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
  };
}

function list(items: GatewayPortForward[]): GatewayPortForwardList {
  return { items };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function renderPanel(api: WorkspacePortForwardApi): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <WorkspacePortForwardPanel
          apiPort={8014}
          workspace={remoteWorkspace}
          active
          api={api}
          confirmStop={async () => true}
        />
      </WarmConfirmProvider>,
    );
  });
  return renderer;
}

function creates(): WorkspacePortForwardApi {
  const payloads: unknown[] = [];
  return {
    list: async () => list([forward()]),
    create: async (_port, _workspaceId, payload) => {
      payloads.push(payload);
      return list([forward()]);
    },
    remove: async () => list([]),
    reconnect: async () => list([]),
    changeLocalPort: async () => list([]),
  };
}

/** 打开新增端口表单并填入远端端口。 */
function openCreateForm(
  renderer: ReactTestRenderer,
  remotePortValue: string,
): void {
  // 新增入口挂在端口列表的右键菜单里，必须先打开表格上下文菜单。
  act(() =>
    renderer.root
      .findByProps({ role: "table" })
      .props.onContextMenu({ preventDefault() {} }),
  );
  act(() =>
    renderer.root
      .findByProps({ "aria-controls": "workspace-port-forward-form" })
      .props.onClick(),
  );
  act(() => {
    renderer.root
      .findByProps({ placeholder: "例如 5173" })
      .props.onChange({ target: { value: remotePortValue } });
  });
}

const INVALID_PORTS = [
  "1e3",
  "12.0",
  " 80 ",
  "0x50",
  "+80",
  "80.5",
  "1e309",
  "abc",
  "0",
  "65536",
];

describe("端口输入只接受 1–65535 十进制整数", () => {
  for (const value of INVALID_PORTS) {
    test(`拒绝非法端口 ${JSON.stringify(value)}`, async () => {
      const renderer = renderPanel(creates());
      await flush();
      openCreateForm(renderer, value);

      // 按钮必须禁用；且必须给出可读原因，不能静默无反应。
      expect(
        renderer.root.findByProps({ className: "port-forward-create" }).props
          .disabled,
      ).toBe(true);
      expect(
        renderer.root.findByProps({ className: "port-forward-port-hint" }).props
          .role,
      ).toBe("alert");
      renderer.unmount();
    });
  }

  test("未填写远端端口时按钮禁用，但不展示校验提示", async () => {
    const renderer = renderPanel(creates());
    await flush();
    openCreateForm(renderer, "");

    expect(
      renderer.root.findByProps({ className: "port-forward-create" }).props
        .disabled,
    ).toBe(true);
    // 空值是「尚未填写」，不是「格式非法」，不该提前弹校验文案。
    expect(
      renderer.root.findAllByProps({ className: "port-forward-port-hint" })
        .length,
    ).toBe(0);
    renderer.unmount();
  });

  for (const value of ["80", "65535", "1"]) {
    test(`接受合法端口 ${value}`, async () => {
      const renderer = renderPanel(creates());
      await flush();
      openCreateForm(renderer, value);

      expect(
        renderer.root.findByProps({ className: "port-forward-create" }).props
          .disabled,
      ).toBe(false);
      expect(
        renderer.root.findAllByProps({ className: "port-forward-port-hint" })
          .length,
      ).toBe(0);
      renderer.unmount();
    });
  }
});

/** 打开某条转发的「更改本地端口」内联表单。 */
function openLocalPortEditor(
  renderer: ReactTestRenderer,
  value: string,
): void {
  const menuItem = renderer.root.findAllByProps({ role: "menuitem" }).find(
    (item) => item.children.includes("更改本地端口"),
  );
  act(() => menuItem!.props.onClick({
    currentTarget: { closest: () => ({ removeAttribute() {} }) },
  }));
  const form = renderer.root.findByProps({ className: "port-forward-edit-form" });
  act(() => form.findByType("input").props.onChange({ target: { value } }));
}

function localPortSaveButton(renderer: ReactTestRenderer) {
  return renderer.root
    .findByProps({ className: "port-forward-edit-form" })
    .findAllByType("button")
    .find((button) => button.props.type === "submit")!;
}

describe("内联更改本地端口与新增端口共享同一份校验反馈", () => {
  for (const value of ["", "1e3", "0", "65536", "abc"]) {
    test(`内联端口非法 ${JSON.stringify(value)} 时保存禁用且给出可见原因`, async () => {
      const renderer = renderPanel(creates());
      await flush();
      openLocalPortEditor(renderer, value);

      const form = renderer.root.findByProps({ className: "port-forward-edit-form" });
      expect(localPortSaveButton(renderer).props.disabled).toBe(true);
      // 禁用按钮必须同时给出原因；否则用户面对「点了没反应」的静默失败。
      expect(
        form.findByProps({ className: "port-forward-port-hint" }).props.role,
      ).toBe("alert");
      renderer.unmount();
    });
  }

  test("内联端口合法时不弹校验提示且保存可用", async () => {
    const renderer = renderPanel(creates());
    await flush();
    openLocalPortEditor(renderer, "41009");

    const form = renderer.root.findByProps({ className: "port-forward-edit-form" });
    expect(localPortSaveButton(renderer).props.disabled).toBe(false);
    expect(
      form.findAllByProps({ className: "port-forward-port-hint" }).length,
    ).toBe(0);
    renderer.unmount();
  });
});
