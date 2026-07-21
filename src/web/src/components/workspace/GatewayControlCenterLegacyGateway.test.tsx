import { renderToString } from "react-dom/server";
import type { GatewayWorkspace } from "../../types/backend";
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
  <GatewayControlCenter
    apiPort={8014}
    workspaces={[legacyWorkspace]}
    activeWorkspaceId={legacyWorkspace.workspace_id}
    recentLocalWorkspacePaths={[]}
    switching={false}
    removingWorkspaceIds={new Set()}
    gatewayError={null}
    onActivate={noop}
    onAddLocal={noop}
    onAddSsh={noop}
    onRemove={() => {}}
    onReorder={noop}
    onRefresh={noop}
    onReconnect={noop}
    onSafeRestartManagedBackend={async () => ({
      workspace_id: "gw_legacy",
      status: "restarted",
      forced: false,
      blockers: [],
      workspaces: { active_workspace_id: null, items: [] },
    })}
    onForceRestartManagedBackend={async () => ({
      workspace_id: "gw_legacy",
      status: "restarted",
      forced: true,
      blockers: [],
      workspaces: { active_workspace_id: null, items: [] },
    })}
    onProbeExternalBackend={noop}
    onRename={async (_workspaceId, name) => name}
    onUseWorkspace={noop}
  />,
);

const versionNotice = "当前 Gateway 进程版本较旧";
if (!html.includes(versionNotice)) {
  throw new Error("旧版 Gateway 数据应正常渲染并显示控制台级版本提示");
}
if (html.includes("需重启 Gateway")) {
  throw new Error("旧版 Gateway 不应在每个工作区显示伪操作按钮");
}
