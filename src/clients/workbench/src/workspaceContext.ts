import { getBrowserUiSettings } from "../../web/src/hooks/settings/browserUiSettings.js";
import type { GatewayWorkspace, WebUiSettings } from '../../web/src/types/backend.js';
import { listGatewayWorkspaces } from '../../web/src/gatewayApi.js';
import { errorMessage } from '../../web/src/utils/errorMessage.js';

interface WorkspaceSnapshot {
  workspaces: GatewayWorkspace[];
  activeId: string | null;
  settings: WebUiSettings | null;
  loading: boolean;
  error: string | null;
}

/** 只镜像 Gateway 已返回的工作区选择，供不同原生视图共享；不持有业务 writer。 */
let snapshot: WorkspaceSnapshot = { workspaces: [], activeId: null, settings: null, loading: true, error: null };
let generation = 0;
const listeners = new Set<() => void>();
export const workspaceContext = {
  getSnapshot: () => snapshot,
  subscribe(listener: () => void): () => void { listeners.add(listener); return () => { listeners.delete(listener); }; },
  publish(next: WorkspaceSnapshot): void {
    generation += 1;
    snapshot = next;
    for (const listener of listeners) listener();
  },
  async refresh(): Promise<void> {
    const request = ++generation;
    try {
      const [workspaces, settings] = await Promise.all([listGatewayWorkspaces(0), getBrowserUiSettings(0)]);
      if (request === generation) this.publish({ workspaces: workspaces.items, activeId: workspaces.active_workspace_id, settings, loading: false, error: null });
    } catch (cause) {
      if (request === generation) this.publish({ ...snapshot, loading: false, error: errorMessage(cause) });
    }
  },
};
