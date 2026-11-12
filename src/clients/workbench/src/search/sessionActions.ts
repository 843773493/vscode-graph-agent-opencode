import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { getSession } from '../../../web/src/api.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceKey, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import type { SessionNavigation } from '../sessionNavigation.js';
import { workspaceContext } from '../workspaceContext.js';
import type { ResourcePick } from './quickAccessPicks.js';
import { captureSearchOwner, type SearchInvocationOptions } from './target.js';
import { chooseSearchOwners } from './quickAccessScope.js';

export const sessionActionCommands = { rename: 'boxteam.session.rename', delete: 'boxteam.session.delete' } as const;
export type SessionAction = keyof typeof sessionActionCommands;
export interface SessionActionTarget { owner: WorkspaceOwner; sessionId: string }
export type SessionActionHandlers = Record<SessionAction,
  (sessionId: string, title: string, workspaceKey: string, assertAccess: () => void) => Promise<void>>;

/** 命令只选择并核验目标；确认、对话框、outbox 和完成对账仍由原导航 owner 持有。 */
async function runSessionAction(accessor: ServicesAccessor, action: SessionAction, supplied?: SessionActionTarget): Promise<void> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService), views = accessor.get(IViewsService);
  const initial = workspaceContext.getSnapshot(), origin = editors.activeEditor;
  if (!initial.gatewayId) throw new Error('Gateway 登记身份尚未加载');
  let target = supplied ? { owner: { ...supplied.owner }, sessionId: supplied.sessionId }
    : origin instanceof ThreadInput ? { owner: { gatewayId: origin.owner.gatewayId, workspaceId: origin.owner.workspaceId }, sessionId: origin.owner.sessionId } : undefined;
  let invalidated = false;
  const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const releaseGateway = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidated = true;
  });
  const assertAccess = () => {
    if (invalidated) throw new Error('Gateway 或访问凭据已改变，请重新选择会话');
  };
  try {
    if (!target) {
      if (initial.loading || initial.error) throw new Error(initial.error ?? '工作区登记信息尚未加载');
      const owners = origin || initial.activeId ? [captureSearchOwner(editors)] : await chooseSearchOwners(quick, []);
      if (!owners) return;
      assertAccess();
      const providerOptions: SearchInvocationOptions = { searchOwners: owners, sessionSelection: true };
      const selected = await quick.quickAccess.pick('session ', { preserveValue: true, enabledProviderPrefixes: ['session '],
        placeholder: '选择会话动作目标 · 不打开 Thread', providerOptions });
      if (!selected?.length) return;
      const picked = (selected[0] as ResourcePick).sessionTarget;
      if (!picked) throw new Error('请选择明确的会话，目录不能作为会话动作目标');
      target = { owner: { ...picked.owner }, sessionId: picked.sessionId };
    }
    assertAccess();
    const registered = await gateway.resolveWorkspaceTarget(target.owner);
    assertAccess();
    if (registered.status !== 'ready') throw new Error(`会话所属工作区当前不可用（${registered.status}），请明确启动或连接后再操作`);
    const workspaceKey = gatewayWorkspaceKey(registered);
    const session = await getSession(0, target.sessionId, workspaceKey);
    assertAccess();
    if (session.session_id !== target.sessionId || session.workspace_id !== target.owner.workspaceId) throw new Error('会话回执与所选归属不一致');
    const view = await views.openView<SessionNavigation>('boxteam.sessions', false);
    assertAccess();
    if (!view) throw new Error('会话导航不可用');
    await view.runSessionAction(action, target.sessionId, session.title, workspaceKey, assertAccess);
  } finally { releaseToken(); releaseGateway(); }
}

for (const [action, title] of [['rename', '重命名会话…'], ['delete', '永久删除会话…']] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: sessionActionCommands[action], title: { value: title, original: title }, f1: true }); }
  run(accessor: ServicesAccessor, target?: SessionActionTarget): Promise<void> { return runSessionAction(accessor, action, target); }
});
