import { useEffect, useState } from 'react';
import PersistentTasks from '../../../web/src/components/workspace/persistentTasks/PersistentTasks.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { TaskThreadTargetDTO } from '../../../web/src/api/persistentTasks.js';
import { getGatewayHealth } from '../../../web/src/gatewayApi.js';
import { gateway, type ThreadOwner } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';

export function TaskView({ owner, sessionId, goalSessionId = sessionId ?? null, editors, active }: {
  owner: Pick<ThreadOwner, 'routeId' | 'workspaceId'>; sessionId?: string; goalSessionId?: string | null;
  editors: IEditorService; active: boolean;
}) {
  const [target, setTarget] = useState<{ route: GatewayWorkspaceRequestTarget; gatewayId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let disposed = false;
    void (async () => {
      await gateway.assertWorkspace(owner);
      const [workspaces, health] = await Promise.all([gateway.workspaces(), getGatewayHealth(0)]);
      const route = requireGatewayWorkspaceRequestTarget(workspaces.items, owner.routeId);
      const workspace = workspaces.items.find(item => item.workspace_id === owner.routeId)!;
      const identity = gatewayWorkspaceOwner(workspace, health.gateway_id);
      if (identity.workspaceId !== owner.workspaceId) throw new Error('任务编辑器的 Gateway 路由归属已改变');
      if (!disposed) setTarget({ route, gatewayId: identity.gatewayId });
    })().catch((cause: unknown) => { if (!disposed) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => { disposed = true; };
  }, [owner]);

  const openThread = async ({ session_id, thread_id, uri }: TaskThreadTargetDTO) => {
    const path = `workspace/${encodeURIComponent(owner.workspaceId)}/resources/session/${encodeURIComponent(session_id)}/threads/${encodeURIComponent(thread_id)}`;
    if (!target || (uri !== `boxteam://${path}` && uri !== `boxteam://${encodeURIComponent(target.gatewayId)}/${path}`)) throw new Error('任务目标 Thread URI 与当前 Gateway、工作区或回执身份不匹配');
    await gateway.assertWorkspace(owner);
    await editors.openEditor(new ThreadInput({ ...owner, gatewayId: target.gatewayId, sessionId: session_id, threadId: thread_id, title: thread_id }), { pinned: true });
  };
  const openSession = async (sessionId: string) => {
    if (!target) throw new Error('任务编辑器尚未确定目标 Gateway 身份');
    const main = await gateway.mainThread(owner.routeId, sessionId);
    await editors.openEditor(new ThreadInput({ ...owner, gatewayId: target.gatewayId, sessionId, threadId: main.thread_id, title: sessionId }), { pinned: true });
  };

  return <WarmConfirmProvider>
    {error ? <p role="alert">任务加载失败：{error}</p> : target ? <PersistentTasks apiPort={0} target={target.route} sessionId={sessionId} goalSessionId={goalSessionId} active={active}
      onStatusChange={setNotice} onOpenSession={openSession} onOpenThread={openThread} /> : <p role="status">正在确认任务所属工作区…</p>}
    {notice ? <p role="status">{notice}</p> : null}
  </WarmConfirmProvider>;
}
