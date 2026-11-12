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
import type { SessionForkSourceSelection } from '../../../web/src/hooks/shell/useSessionForkDialog.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';

export const sessionActionCommands = { rename: 'boxteam.session.rename', fork: 'boxteam.session.fork', delete: 'boxteam.session.delete' } as const;
export type SessionAction = keyof typeof sessionActionCommands;
export interface SessionActionTarget { owner: WorkspaceOwner; sessionId: string; source?: SessionForkSourceSelection }
export type SessionActionHandlers = Record<SessionAction,
  (sessionId: string, title: string, workspaceKey: string, assertAccess: () => void, source?: SessionForkSourceSelection) => Promise<void>>;

/** 命令只选择并核验目标；确认、对话框、outbox 和完成对账仍由原导航 owner 持有。 */
async function runSessionAction(accessor: ServicesAccessor, action: SessionAction, supplied?: SessionActionTarget): Promise<void> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService), views = accessor.get(IViewsService);
  const initial = workspaceContext.getSnapshot(), origin = editors.activeEditor;
  if (!initial.gatewayId) throw new Error(t('workbench.sessionAction.gatewayPending'));
  let target: SessionActionTarget | undefined = supplied ? { ...supplied, owner: { ...supplied.owner }, source: supplied.source ? { ...supplied.source } : undefined }
    : origin instanceof ThreadInput ? { owner: { gatewayId: origin.owner.gatewayId, workspaceId: origin.owner.workspaceId }, sessionId: origin.owner.sessionId,
      source: { threadId: origin.owner.threadId } } : undefined;
  let invalidated = false;
  const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const releaseGateway = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidated = true;
  });
  const assertAccess = () => {
    if (invalidated) throw new Error(t('workbench.sessionAction.identityChanged'));
  };
  try {
    if (!target) {
      if (initial.loading || initial.error) throw new Error(initial.error ?? t('workbench.sessionAction.registrationPending'));
      const owners = origin || initial.activeId ? [captureSearchOwner(editors)] : await chooseSearchOwners(quick, []);
      if (!owners) return;
      assertAccess();
      const providerOptions: SearchInvocationOptions = { searchOwners: owners, sessionSelection: true };
      const selected = await quick.quickAccess.pick('session ', { preserveValue: true, enabledProviderPrefixes: ['session '],
        placeholder: t('workbench.sessionAction.pickPlaceholder'), providerOptions });
      if (!selected?.length) return;
      const picked = (selected[0] as ResourcePick).sessionTarget;
      if (!picked) throw new Error(t('workbench.sessionAction.sessionRequired'));
      target = { owner: { ...picked.owner }, sessionId: picked.sessionId };
    }
    assertAccess();
    const registered = await gateway.resolveWorkspaceTarget(target.owner);
    assertAccess();
    if (registered.status !== 'ready') throw new Error(t('workbench.sessionAction.workspaceUnavailable', registered.status));
    const workspaceKey = gatewayWorkspaceKey(registered);
    const session = await getSession(0, target.sessionId, workspaceKey);
    assertAccess();
    if (session.session_id !== target.sessionId || session.workspace_id !== target.owner.workspaceId) throw new Error(t('workbench.sessionAction.receiptMismatch'));
    const view = await views.openView<SessionNavigation>('boxteam.sessions', false);
    assertAccess();
    if (!view) throw new Error(t('workbench.sessionAction.navigationUnavailable'));
    await view.runSessionAction(action, target.sessionId, session.title, workspaceKey, assertAccess, action === 'fork' ? target.source : undefined);
  } finally { releaseToken(); releaseGateway(); }
}

for (const [action, titleKey] of [
  ['rename', 'workbench.sessionAction.rename'],
  ['fork', 'workbench.sessionAction.fork'],
  ['delete', 'workbench.sessionAction.delete'],
] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: sessionActionCommands[action], title: { value: t(titleKey), original: actionTitles[titleKey] }, f1: true }); }
  run(accessor: ServicesAccessor, target?: SessionActionTarget): Promise<void> { return runSessionAction(accessor, action, target); }
});
