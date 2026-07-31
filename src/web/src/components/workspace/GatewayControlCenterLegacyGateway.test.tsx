import { renderToString } from "react-dom/server";
import type { GatewayWorkspace } from "../../types/backend";
import WarmConfirmProvider from "../WarmConfirmProvider";
import GatewayControlCenter from "./GatewayControlCenter";

const legacyWorkspace: GatewayWorkspace = {
  workspace_id: "gw_legacy",
  name: "旧版 Gateway 工作区",
  root_path: "/workspace/legacy",
  backend_url: "http://127.0.0.1:8010",
  connection_kind: "local",
  status: "ready",
  active: true,
  managed: false,
  removable: false,
  system_default: true,
  remote: null,
  services: {},
  checked_at: "2026-07-19T00:00:00Z",
};

const noop = async () => {};
const html = renderToString(
  <WarmConfirmProvider>
    <GatewayControlCenter
      apiPort={8014}
      workspaces={[legacyWorkspace]}
      gatewayError={null}
      onAddSsh={noop}
      onRefresh={noop}
      onReconnect={noop}
    />
  </WarmConfirmProvider>,
);

if (!html.includes("连接管理")) {
  throw new Error("旧版 Gateway 数据应正常渲染连接管理页面");
}
if (html.includes("工作区与路由") || html.includes("添加工作区")) {
  throw new Error("连接管理页面不应继续承载工作区管理职责");
}
