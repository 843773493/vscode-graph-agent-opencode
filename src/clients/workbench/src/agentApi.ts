import type { Session, SessionMainThread } from '../../web/src/types/protocol.js';
import type { APIResponse, GatewayWorkspace } from '../../web/src/types/backend.js';
import { requestJson, unwrapApiDataOrNull, workspaceHeader } from '../../web/src/api/http.js';
import { getGatewayHealth } from '../../web/src/gatewayApi.js';
import { requireGatewayWorkspaceRoute, type WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
export { HttpRequestError as GatewayRequestError } from '../../web/src/api/http.js';

export type WorkspaceTarget = GatewayWorkspace;
export interface ThreadOwner extends WorkspaceOwner {
  routeId: string;
  workspaceId: string;
  sessionId: string;
  threadId: string;
  title: string;
}
interface Page<T> { items: T[]; next_cursor?: string | null; has_more?: boolean }

class GatewayClient {
  async request<T>(path: string, options: RequestInit = {}, routeId?: string): Promise<T> {
    const headers = new Headers(options.headers);
    for (const [key, value] of Object.entries(workspaceHeader(routeId))) headers.set(key, value);
    // 所有业务贡献共用既有凭据、用户屏障和错误协议，不再维护第二套初始化。
    const body = await requestJson<APIResponse<T> & { data: T }>(0, path, { ...options, headers });
    unwrapApiDataOrNull(body);
    if (body.code !== 0) throw new Error(`${path}：${body.message}（${body.request_id}）`);
    return body.data;
  }

  workspaces(checkHealth = true, signal?: AbortSignal): Promise<{ items: WorkspaceTarget[]; active_workspace_id: string }> {
    return this.request(`/api/gateway/workspaces?check_health=${checkHealth}`, { signal });
  }

  async resolveWorkspace(owner: WorkspaceOwner, signal?: AbortSignal): Promise<string> {
    return (await this.resolveWorkspaceTarget(owner, signal)).workspace_id;
  }

  async resolveWorkspaceTarget(owner: WorkspaceOwner, signal?: AbortSignal): Promise<WorkspaceTarget> {
    // 重解只读取控制面登记，不因切换标签探测所有工作区及辅助服务。
    const routes = await this.workspaceRoutes(signal);
    return requireGatewayWorkspaceRoute(routes.items, routes.gatewayId, owner);
  }

  async workspaceRoutes(signal?: AbortSignal): Promise<{ items: WorkspaceTarget[]; gatewayId: string }> {
    const [workspaces, health] = await Promise.all([this.workspaces(false, signal), getGatewayHealth(0, { signal })]);
    return { items: workspaces.items, gatewayId: health.gateway_id };
  }

  async assertWorkspace(owner: Pick<ThreadOwner, 'routeId' | 'workspaceId'>): Promise<void> {
    const workspace = await this.request<{ workspace_id: string }>('/api/v1/workspace', {}, owner.routeId);
    if (workspace.workspace_id !== owner.workspaceId) throw new Error('Gateway 路由对应的工作区已改变，停止操作');
  }

  async sessions(routeId: string): Promise<Session[]> {
    const items: Session[] = [];
    let cursor: string | undefined;
    do {
      const page = await this.request<Page<Session>>(`/api/v1/sessions?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, {}, routeId);
      items.push(...page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    return items;
  }

  async mainThread(routeId: string, sessionId: string): Promise<SessionMainThread> {
    const result = await this.request<SessionMainThread>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/main-thread`, {}, routeId);
    if (result.session_id !== sessionId || !result.thread_id) throw new Error('会话主 Thread 响应 owner 不匹配');
    return result;
  }

}
export const gateway = new GatewayClient();
