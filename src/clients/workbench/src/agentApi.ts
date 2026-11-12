import { gatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import type { Session, SessionMainThread } from '../../web/src/types/protocol.js';
import type { APIResponse, GatewayWorkspaceList, GatewayWorkspace } from '../../web/src/types/backend.js';
import { requestJson, unwrapApiDataOrNull, workspaceHeader } from '../../web/src/api/http.js';
import { requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
export { HttpRequestError as GatewayRequestError } from '../../web/src/api/http.js';

export type WorkspaceTarget = GatewayWorkspace;
export interface ThreadOwner extends WorkspaceOwner {
  workspaceKey: string;
  workspaceId: string;
  sessionId: string;
  threadId: string;
  title: string;
}
interface Page<T> { items: T[]; next_cursor?: string | null; has_more?: boolean }

class GatewayClient {
  async request<T>(path: string, options: RequestInit & { onResponse?: (requestId: string | null) => void } = {}, workspaceKey?: string): Promise<T> {
    const headers = new Headers(options.headers);
    for (const [key, value] of Object.entries(workspaceHeader(workspaceKey))) headers.set(key, value);
    // 所有业务贡献共用既有本机凭据、请求屏障和错误协议，不再维护第二套初始化。
    const body = await requestJson<APIResponse<T> & { data: T }>(0, path, { ...options, headers });
    unwrapApiDataOrNull(body);
    if (body.code !== 0) throw new Error(`${path}：${body.message}（${body.request_id}）`);
    return body.data;
  }

  workspaces(checkHealth = true, signal?: AbortSignal): Promise<GatewayWorkspaceList> {
    return this.request(`/api/gateway/workspaces?check_health=${checkHealth}`, { signal });
  }

  async resolveWorkspace(owner: WorkspaceOwner, signal?: AbortSignal): Promise<string> {
    return gatewayWorkspaceKey(await this.resolveWorkspaceTarget(owner, signal));
  }

  async resolveWorkspaceTarget(owner: WorkspaceOwner, signal?: AbortSignal): Promise<WorkspaceTarget> {
    // 重解只读取控制面登记，不因切换标签探测所有工作区及辅助服务。
    const routes = await this.workspaces(false, signal);
    return requireGatewayWorkspaceRequestTarget(routes.items, owner);
  }


  async assertWorkspace(owner: Pick<ThreadOwner, 'workspaceKey' | 'workspaceId'>): Promise<void> {
    const workspace = await this.request<{ workspace_id: string }>('/api/v1/workspace', {}, owner.workspaceKey);
    if (workspace.workspace_id !== owner.workspaceId) throw new Error('Gateway 路由对应的工作区已改变，停止操作');
  }

  async sessions(workspaceKey: string): Promise<Session[]> {
    const items: Session[] = [];
    let cursor: string | undefined;
    do {
      const page = await this.request<Page<Session>>(`/api/v1/sessions?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, {}, workspaceKey);
      items.push(...page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    return items;
  }

  async mainThread(workspaceKey: string, sessionId: string): Promise<SessionMainThread> {
    const result = await this.request<SessionMainThread>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/main-thread`, {}, workspaceKey);
    if (result.session_id !== sessionId || !result.thread_id) throw new Error('会话主 Thread 响应 owner 不匹配');
    return result;
  }

}
export const gateway = new GatewayClient();
