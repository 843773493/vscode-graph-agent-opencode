import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { gatewayWorkspaceOwner, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { t } from '../../../shared/core/i18n/index.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';

export function searchOwnerKey(owner: WorkspaceOwner): string { return `${owner.gatewayId}/${owner.workspaceId}`; }

/** 只读取已有控制面镜像；浏览和勾选不会访问任何工作区业务接口。 */
export async function chooseSearchOwners(quickInput: IQuickInputService, current: readonly WorkspaceOwner[], token?: CancellationToken): Promise<WorkspaceOwner[] | undefined> {
  const snapshot = workspaceContext.getSnapshot();
  if (snapshot.loading || snapshot.error || !snapshot.gatewayId) throw new Error(snapshot.error ?? t('quickAccess.scope.registrationPending'));
  const selected = new Set(current.map(searchOwnerKey));
  const picks = snapshot.workspaces.map(target => {
    const owner = gatewayWorkspaceOwner(target);
    return { label: target.name, description: `${owner.gatewayId} · ${t(target.status === 'ready' ? 'quickAccess.scope.ready' : 'quickAccess.scope.offline')}`, detail: owner.workspaceId,
      picked: selected.has(searchOwnerKey(owner)), owner };
  });
  const result = await quickInput.pick(picks, { canPickMany: true, title: t('quickAccess.scope.title'),
    placeHolder: t('quickAccess.scope.hint') }, token);
  if (!result) return undefined;
  if (!result.length) throw new Error(t('quickAccess.scope.requireWorkspace'));
  return [...new Map(result.map(item => [searchOwnerKey(item.owner), item.owner])).values()];
}
