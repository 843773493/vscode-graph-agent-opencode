import { useState, useSyncExternalStore } from 'react';
import { t } from '../../../shared/core/i18n/index.js';
import PersistentTasks from '../../../web/src/components/workspace/persistentTasks/PersistentTasks.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { TaskThreadTargetDTO } from '../../../web/src/api/persistentTasks.js';
import { gateway, type ThreadOwner } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { commands } from '../../upstream/src/vs/workbench/browser/web.factory.js';
import { taskActionCommand } from '../automation/actions.js';
import type { PersistentTaskDTO } from '../../../web/src/api/persistentTasks.js';
import { workspaceContext } from '../workspaceContext.js';
import type { TaskPresentationAction } from '../automation/input.js';

export function TaskView({ owner, sessionId, goalSessionId = sessionId ?? null, editors, active, intent }: {
  owner: Pick<ThreadOwner, 'workspaceKey' | 'workspaceId'>; sessionId?: string; goalSessionId?: string | null;
  editors: IEditorService; active: boolean;
  intent?: { task: PersistentTaskDTO; action: TaskPresentationAction; sequence: number };
}) {
  const snapshot = useSyncExternalStore(workspaceContext.subscribe, workspaceContext.getSnapshot);
  const [notice, setNotice] = useState('');
  let target: { route: GatewayWorkspaceRequestTarget; gatewayId: string } | null = null;
  let error = snapshot.error;
  if (snapshot.gatewayId) {
    try {
      const route = requireGatewayWorkspaceRequestTarget(snapshot.workspaces, owner.workspaceKey);
      const identity = gatewayWorkspaceOwner(route);
      if (identity.workspaceId !== owner.workspaceId) throw new Error(t('workbench.automation.routeOwnerChanged'));
      target = { route, gatewayId: identity.gatewayId };
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  const openThread = async ({ session_id, thread_id, uri }: TaskThreadTargetDTO) => {
    const path = `workspace/${encodeURIComponent(owner.workspaceId)}/resources/session/${encodeURIComponent(session_id)}/threads/${encodeURIComponent(thread_id)}`;
    if (!target || (uri !== `boxteam://${path}` && uri !== `boxteam://${encodeURIComponent(target.gatewayId)}/${path}`)) throw new Error(t('workbench.automation.threadUriMismatch'));
    await gateway.assertWorkspace(owner);
    await editors.openEditor(new ThreadInput({ ...owner, gatewayId: target.gatewayId, sessionId: session_id, threadId: thread_id, title: thread_id }), { pinned: true });
  };
  const openSession = async (sessionId: string) => {
    if (!target) throw new Error(t('workbench.automation.gatewayMissing'));
    const main = await gateway.mainThread(owner.workspaceKey, sessionId);
    await editors.openEditor(new ThreadInput({ ...owner, gatewayId: target.gatewayId, sessionId, threadId: main.thread_id, title: sessionId }), { pinned: true });
  };

  return <WarmConfirmProvider><div className="task-view panel-feedback-host" aria-busy={snapshot.loading}>
    <div className="panel-feedback">
      {error ? <p role="alert">{t('workbench.automation.loadFailed', error)}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
    </div>
    {target ? <PersistentTasks apiPort={0} target={target.route} sessionId={sessionId} goalSessionId={goalSessionId} active={active}
      intent={intent} onAction={async (action, source, configuration) => (await commands.executeCommand(taskActionCommand(action),
        { owner: gatewayWorkspaceOwner(source.target), task: source.task, source, configuration })) === true}
      onStatusChange={setNotice} onOpenSession={openSession} onOpenThread={openThread} /> : null}
  </div></WarmConfirmProvider>;
}
