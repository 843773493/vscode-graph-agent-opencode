import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { useCallback, useEffect, useState } from 'react';
import { getSessionCatalogBreadcrumb } from '../../api/session/sessionCatalog';
import type { GatewayWorkspaceRequestTarget } from '../../api/workspaceIdentity';
import type { SessionResourceExplorerController } from '../session/useSessionResourceExplorer';

/** 单次展示意图；目录身份与祖先仍从原 catalog 读取。 */
export interface CatalogRevealRequest {
  target: GatewayWorkspaceRequestTarget;
  nodeId: string;
  focus: boolean;
  signal: AbortSignal;
  complete: () => void;
  fail: (error: unknown) => void;
}

/** 等待真实分支加载及目标按钮挂载后再完成定位，不使用搜索时的旧 breadcrumb。 */
export function useCatalogReveal(apiPort: number, request: CatalogRevealRequest | undefined,
  explorer: SessionResourceExplorerController) {
  const [mountedRequest, setMountedRequest] = useState<CatalogRevealRequest>();
  const { navigation, navigationError, revealSearchResult } = explorer;
  const ready = navigation !== null && navigation !== undefined;
  useEffect(() => {
    if (!request || request.signal.aborted) return;
    if (navigationError) { request.fail(new Error(navigationError)); return; }
    if (!ready) return;
    let active = true;
    void getSessionCatalogBreadcrumb(apiPort, request.target, request.nodeId, { signal: request.signal })
      .then(async breadcrumb => {
        if (!active) return;
        request.signal.throwIfAborted();
        if (breadcrumb.items.at(-1)?.node_id !== request.nodeId || breadcrumb.items.at(-1)?.kind !== 'folder') {
          throw new Error(`文件夹命中已失效：${request.nodeId}`);
        }
        await revealSearchResult(gatewayWorkspaceKey(request.target), breadcrumb.items.map(item => item.node_id), 'folder');
        if (active && !request.signal.aborted) setMountedRequest(request);
      })
      .catch(error => { if (active) request.fail(error); });
    return () => { active = false; };
  }, [apiPort, request, ready, navigationError, revealSearchResult]);

  const reveal = useCallback((element: HTMLButtonElement | null) => {
    if (!element || !request || mountedRequest !== request || request.signal.aborted) return;
    if (element.disabled) { request.fail(new Error(`文件夹正在删除：${request.nodeId}`)); return; }
    element.scrollIntoView({ block: 'nearest' });
    if (request.focus) element.focus({ preventScroll: true });
    request.complete();
  }, [mountedRequest, request]);
  return { workspaceId: request ? gatewayWorkspaceKey(request.target) : undefined, nodeId: request?.nodeId, reveal };
}
