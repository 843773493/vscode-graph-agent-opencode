import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import { getBrowserUiSettings } from "../../web/src/hooks/settings/browserUiSettings.js";
import type { GatewayWorkspace, WebUiSettings } from '../../web/src/types/backend.js';
import { listGatewayWorkspaces } from '../../web/src/gatewayApi.js';
import { errorMessage } from '../../web/src/utils/errorMessage.js';
import { browserGatewayId } from '../../web/src/hooks/settings/browserUiSettings.js';
import { gatewayWorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';

interface WorkspaceSnapshot {
  gatewayId: string | null;
  workspaces: GatewayWorkspace[];
  activeId: string | null;
  settings: WebUiSettings | null;
  loading: boolean;
  error: string | null;
}

/** 只镜像 Gateway 已返回的工作区选择，供不同原生视图共享；不持有业务 writer。 */
let snapshot: WorkspaceSnapshot = { gatewayId: null, workspaces: [], activeId: null, settings: null, loading: true, error: null };
let generation = 0;
const listeners = new Set<() => void>();
export const workspaceContext = {
  getSnapshot: () => snapshot,
  ownerFor(workspaceKey: string, workspaceId: string) {
    const target = snapshot.workspaces.find(item => gatewayWorkspaceKey(item) === workspaceKey);
    if (!snapshot.gatewayId || !target) throw new Error('工作区登记信息尚不可用，无法确定编辑器归属');
    const owner = gatewayWorkspaceOwner(target);
    if (owner.workspaceId !== workspaceId) throw new Error('工作区路由归属已改变，停止打开编辑器');
    return { ...owner, workspaceKey };
  },
  subscribe(listener: () => void): () => void { listeners.add(listener); return () => { listeners.delete(listener); }; },
  publish(next: WorkspaceSnapshot): void {
    generation += 1;
    snapshot = next;
    for (const listener of listeners) listener();
  },
  async refresh(): Promise<void> {
    const request = ++generation;
    try {
      const [workspaces, settings, gatewayId] = await Promise.all([listGatewayWorkspaces(0), getBrowserUiSettings(0), browserGatewayId(0)]);
      if (request === generation) this.publish({ gatewayId, workspaces: workspaces.items, activeId: nullableGatewayWorkspaceKey(workspaces.active_workspace), settings, loading: false, error: null });
    } catch (cause) {
      if (request === generation) this.publish({ ...snapshot, loading: false, error: errorMessage(cause) });
    }
  },
};
