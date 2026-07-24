import { useMemo, useRef, useState } from "react";
import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayWorkspace,
} from "../../types/backend";
import WorkspaceLocalDialog from "./WorkspaceLocalDialog";
import WorkspaceSshDialog from "./WorkspaceSshDialog";
import AnchoredOverlay from "../AnchoredOverlay";

interface WorkspaceSwitcherProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  recentLocalWorkspacePaths: string[];
  switching: boolean;
  onActivate: (workspaceId: string) => Promise<void>;
  onAddLocal: (payload: AddLocalGatewayWorkspaceRequest) => Promise<void>;
  onAddSsh: (payload: AddSshGatewayWorkspaceRequest) => Promise<void>;
}

function workspaceLabel(workspace: GatewayWorkspace | undefined): string {
  if (!workspace) {
    return "workspace";
  }
  return workspace.name || workspace.root_path || workspace.workspace_id;
}

function workspaceKindLabel(workspace: GatewayWorkspace | undefined): string {
  if (!workspace) {
    return "工作区";
  }
  if (workspace.connection_kind === "local") {
    return "本地";
  }
  return "远程 Gateway";
}

export default function WorkspaceSwitcher({
  apiPort,
  workspaces,
  activeWorkspaceId,
  recentLocalWorkspacePaths,
  switching,
  onActivate,
  onAddLocal,
  onAddSsh,
}: WorkspaceSwitcherProps) {
  const [open, setOpen] = useState(false);
  const [localDialogOpen, setLocalDialogOpen] = useState(false);
  const [sshDialogOpen, setSshDialogOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === activeWorkspaceId),
    [activeWorkspaceId, workspaces],
  );

  const handleAddLocal = () => {
    setOpen(false);
    setLocalDialogOpen(true);
  };

  const handleAddSsh = () => {
    setOpen(false);
    setSshDialogOpen(true);
  };

  return (
    <>
      <div className="workspace-switcher">
        <button
          ref={buttonRef}
          type="button"
          className={`workspace-switcher-button${switching ? " switching" : ""}`}
          disabled={switching}
          title={switching ? "正在切换工作区" : (activeWorkspace?.root_path ?? "选择工作区")}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="workspace-switcher-kind">
            {workspaceKindLabel(activeWorkspace)}
          </span>
          <span className="workspace-switcher-label">
            {switching ? "切换中" : workspaceLabel(activeWorkspace)}
          </span>
          <span className="workspace-switcher-chevron" aria-hidden="true">⌄</span>
        </button>
        <AnchoredOverlay
          open={open}
          anchorRef={buttonRef}
          placement="bottom-end"
          onClose={() => setOpen(false)}
        >
          <div className="workspace-switcher-menu" role="menu">
            <div className="workspace-switcher-menu-section">
              {workspaces.map((workspace) => (
                <button
                  key={workspace.workspace_id}
                  type="button"
                  className={`workspace-switcher-item${workspace.workspace_id === activeWorkspaceId ? " active" : ""}`}
                  role="menuitem"
                  title={workspace.connection_error ?? undefined}
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
                    {workspaceKindLabel(workspace)} ·{" "}
                    {workspace.root_path}
                  </span>
                </button>
              ))}
            </div>
            <div className="workspace-switcher-menu-actions">
              <button type="button" onClick={handleAddLocal}>添加本机工作区</button>
              <button type="button" onClick={handleAddSsh}>连接远程 Gateway</button>
            </div>
          </div>
        </AnchoredOverlay>
      </div>
      <WorkspaceLocalDialog
        open={localDialogOpen}
        apiPort={apiPort}
        workspaces={workspaces}
        activeWorkspaceId={activeWorkspaceId}
        recentLocalWorkspacePaths={recentLocalWorkspacePaths}
        onClose={() => setLocalDialogOpen(false)}
        onSubmit={onAddLocal}
      />
      <WorkspaceSshDialog
        open={sshDialogOpen}
        apiPort={apiPort}
        onClose={() => setSshDialogOpen(false)}
        onSubmit={onAddSsh}
      />
    </>
  );
}
