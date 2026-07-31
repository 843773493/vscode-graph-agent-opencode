import { useMemo, useRef, useState } from "react";
import type { GatewayWorkspace } from "../../types/backend";
import AnchoredOverlay from "../AnchoredOverlay";

interface WorkspaceSwitcherProps {
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  switching: boolean;
  onActivate: (workspaceId: string) => Promise<void>;
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
  workspaces,
  activeWorkspaceId,
  switching,
  onActivate,
}: WorkspaceSwitcherProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === activeWorkspaceId),
    [activeWorkspaceId, workspaces],
  );

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
                  title={workspace.status === "offline" ? "请在会话工作台右击工作区并启动" : workspace.connection_error ?? undefined}
                  disabled={switching || workspace.status === "offline"}
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
          </div>
        </AnchoredOverlay>
      </div>
    </>
  );
}
