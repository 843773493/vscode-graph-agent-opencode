import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayWorkspace,
} from "../../types/backend";

interface WorkspaceSwitcherProps {
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  switching: boolean;
  onActivate: (workspaceId: string) => Promise<void>;
  onAddLocal: (payload: AddLocalGatewayWorkspaceRequest) => Promise<void>;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
}

function promptRequired(label: string, defaultValue = ""): string | null {
  const value = window.prompt(label, defaultValue);
  if (value === null) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function promptOptional(label: string, defaultValue = ""): string | null {
  const value = window.prompt(label, defaultValue);
  if (value === null) {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function workspaceLabel(workspace: GatewayWorkspace | undefined): string {
  if (!workspace) {
    return "workspace";
  }
  return workspace.name || workspace.root_path || workspace.workspace_id;
}

export default function WorkspaceSwitcher({
  workspaces,
  activeWorkspaceId,
  switching,
  onActivate,
  onAddLocal,
  onAddSsh,
}: WorkspaceSwitcherProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === activeWorkspaceId),
    [activeWorkspaceId, workspaces],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  const handleAddLocal = () => {
    const rootPath = promptRequired("本机工作区绝对路径");
    if (!rootPath) {
      return;
    }
    const name = promptOptional("工作区名称（可留空）");
    const backendUrl = promptOptional("已有后端 URL（可留空，由 Gateway 启动）");
    setOpen(false);
    void onAddLocal({
      root_path: rootPath,
      name,
      backend_url: backendUrl,
    });
  };

  const handleAddSsh = () => {
    const host = promptRequired("SSH 主机");
    if (!host) return;
    const username = promptRequired("SSH 用户名");
    if (!username) return;
    const privateKeyPath = promptRequired("本机私钥路径");
    if (!privateKeyPath) return;
    const remoteWorkspacePath = promptRequired("要添加的远程工作区路径");
    if (!remoteWorkspacePath) return;
    const remoteBackendPortText = promptRequired("远程后端端口", "8010");
    if (!remoteBackendPortText) return;
    const remoteBackendPort = Number.parseInt(remoteBackendPortText, 10);
    if (!Number.isInteger(remoteBackendPort) || remoteBackendPort < 1 || remoteBackendPort > 65535) {
      window.alert("远程后端端口必须是 1-65535 的整数");
      return;
    }
    const sshPortText = promptOptional("SSH 端口", "22") ?? "22";
    const sshPort = Number.parseInt(sshPortText, 10);
    if (!Number.isInteger(sshPort) || sshPort < 1 || sshPort > 65535) {
      window.alert("SSH 端口必须是 1-65535 的整数");
      return;
    }
    const name = promptOptional("工作区名称（可留空）");
    const remoteBackendHost = promptOptional("远程后端监听地址", "127.0.0.1") ?? "127.0.0.1";
    setOpen(false);
    void onAddSsh({
      name,
      host,
      port: sshPort,
      username,
      private_key_path: privateKeyPath,
      remote_backend_host: remoteBackendHost,
      remote_backend_port: remoteBackendPort,
      remote_workspace_path: remoteWorkspacePath,
    });
  };

  return (
    <div className="workspace-switcher" ref={menuRef}>
      <button
        type="button"
        className="workspace-switcher-button"
        disabled={switching}
        title={activeWorkspace?.root_path ?? "选择工作区"}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="workspace-switcher-kind">
          {activeWorkspace?.connection_kind === "ssh" ? "SSH" : "本地"}
        </span>
        <span className="workspace-switcher-label">
          {switching ? "切换中" : workspaceLabel(activeWorkspace)}
        </span>
        <span className="workspace-switcher-chevron" aria-hidden="true">⌄</span>
      </button>
      {open ? (
        <div className="workspace-switcher-menu" role="menu">
          <div className="workspace-switcher-menu-section">
            {workspaces.map((workspace) => (
              <button
                key={workspace.workspace_id}
                type="button"
                className={`workspace-switcher-item${workspace.workspace_id === activeWorkspaceId ? " active" : ""}`}
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  if (workspace.workspace_id !== activeWorkspaceId) {
                    void onActivate(workspace.workspace_id);
                  }
                }}
              >
                <span className="workspace-switcher-item-title">
                  {workspace.name}
                  <span className={`workspace-switcher-status ${workspace.status}`} />
                </span>
                <span className="workspace-switcher-item-path">
                  {workspace.connection_kind === "ssh" ? "SSH " : ""}
                  {workspace.root_path}
                </span>
              </button>
            ))}
          </div>
          <div className="workspace-switcher-menu-actions">
            <button type="button" onClick={handleAddLocal}>添加本机工作区</button>
            <button type="button" onClick={handleAddSsh}>添加 SSH 工作区</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
