import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { gatewayWorkspaceOwner, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';

export function searchOwnerKey(owner: WorkspaceOwner): string { return `${owner.gatewayId}/${owner.workspaceId}`; }

/** 只读取已有控制面镜像；浏览和勾选不会访问任何工作区业务接口。 */
export async function chooseSearchOwners(quickInput: IQuickInputService, current: readonly WorkspaceOwner[]): Promise<WorkspaceOwner[] | undefined> {
  const snapshot = workspaceContext.getSnapshot();
  if (snapshot.loading || snapshot.error || !snapshot.gatewayId) throw new Error(snapshot.error ?? '工作区登记信息尚未加载');
  const gatewayId = snapshot.gatewayId;
  const selected = new Set(current.map(searchOwnerKey));
  const picks = snapshot.workspaces.map(target => {
    const owner = gatewayWorkspaceOwner(target, gatewayId);
    return { label: target.name, description: `${owner.gatewayId} · ${target.status}`, detail: owner.workspaceId,
      picked: selected.has(searchOwnerKey(owner)), owner };
  });
  const result = await quickInput.pick(picks, { canPickMany: true, title: '选择快速访问搜索范围',
    placeHolder: '确认后仅查询选定工作区；不会自动启动、连接或激活' });
  if (!result) return undefined;
  if (!result.length) throw new Error('搜索范围至少需要一个工作区');
  return [...new Map(result.map(item => [searchOwnerKey(item.owner), item.owner])).values()];
}
