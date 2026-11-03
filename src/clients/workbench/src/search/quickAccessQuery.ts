import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { QueryBuilder } from '../../upstream/src/vs/workbench/services/search/common/queryBuilder.js';
import type { WorkspaceSearchService } from './searchService.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { SessionCatalogSearchResults } from '../../../web/src/types/backend.js';
import type { WorkspaceMessageSearchResultsDTO, TurnMessageMatchDTO, WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gatewayWorkspaceRequestHeaders } from '../../../web/src/api/http.js';
import { searchWorkspaceHistoryMessages } from '../../../web/src/api/session/sessionTurnHistory.js';
import { searchSessionThreads } from '../../../web/src/api/session/sessions.js';
import { gateway, type ThreadOwner, type WorkspaceTarget } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import { SearchWorkspaceContext } from './scope.js';

export type NameSearchCategory = '会话' | 'Thread' | '文件';
export type SearchCategory = NameSearchCategory | '会话消息';
export type ResourceSearchItem = { title: string; description: string; detail: string } & (
  { kind: 'resource'; resource: URI; message?: TurnMessageMatchDTO } | { kind: 'session'; sessionId: string } | { kind: 'folder'; nodeId: string }
);
export interface ResourceSearchPage {
  items: ResourceSearchItem[];
  cursor?: string;
  limited?: string;
  requestIds: string[];
}

/** 查询只读取所选工作区原 owner；不使用 Gateway 全范围聚合或前端全量目录。 */
export async function searchQuickAccessResources(category: SearchCategory, filter: string,
  owner: WorkspaceOwner, target: WorkspaceTarget, label: string, signal: AbortSignal, token: CancellationToken,
  instantiation: IInstantiationService, search: WorkspaceSearchService, cursor?: string, deadlineAt?: number): Promise<ResourceSearchPage> {
  const query = new URLSearchParams({ query: filter, limit: '50' });
  const requestIds: string[] = [];
  const onResponse = (requestId: string | null) => { if (requestId) requestIds.push(requestId); };
  if (cursor) query.set('cursor', cursor);
  if (category === '会话消息') {
    const page: WorkspaceMessageSearchResultsDTO = await searchWorkspaceHistoryMessages(0, { query: filter, limit: 50, cursor },
      target, { signal, deadlineAt, onResponse });
    return { cursor: page.cursor ?? undefined, requestIds,
      limited: page.complete ? undefined : `本次正文覆盖受限（${page.limited_reason ?? '尚未覆盖全部历史'}）`,
      items: page.items.map(({ thread, match }) => {
        if (!thread || !match?.reference || thread.workspace_id !== owner.workspaceId) throw new Error('消息命中缺少精确归属或位置');
        const input = new ThreadInput({ ...owner, routeId: '', sessionId: thread.session_id, threadId: thread.thread_id, title: thread.title });
        return { kind: 'resource', resource: input.resource, message: match,
          title: thread.title, description: match.preview.replace(/\s+/g, ' '),
          detail: `${label} · ${thread.session_title} · ${match.role === 'user' ? '用户' : '助手'}消息 · ${thread.session_id} / ${thread.thread_id} · ${match.turn_id}` };
      }) };
  }

  if (category === '会话') {
    const page = await gateway.request<SessionCatalogSearchResults>(`/api/v1/session-catalog/search?${query}`,
      { signal, onResponse, headers: { ...gatewayWorkspaceRequestHeaders(target),
        ...(deadlineAt !== undefined ? { 'X-BoxTeam-Name-Search-Deadline': String(deadlineAt / 1000) } : {}) } }, target.workspace_id);
    return { cursor: page.cursor ?? undefined, requestIds, items: page.items.map((item): ResourceSearchItem => {
      if (item.node.kind === 'folder') return { kind: 'folder', nodeId: item.node.node_id, title: item.node.name,
        description: `${label} · ${item.relative_path} · ${item.node.node_id}`, detail: `文件夹 · ${item.node.node_id}` };
      if (!item.node.session_id) throw new Error('会话目录命中缺少精确 Session 身份');
      return { kind: 'session', sessionId: item.node.session_id, title: item.node.name, description: `${label} · ${item.relative_path} · ${item.node.session_id}`,
        detail: `会话 · ${item.node.session_id}` };
    }) };
  }
  if (category === 'Thread') {
    const page = await searchSessionThreads(0, target, filter, { signal, onResponse, limit: 50, cursor, deadlineAt });
    return { cursor: page.cursor, requestIds, limited: page.complete ? undefined : page.limited_reason ?? '尚未覆盖全部 Thread',
      items: page.items.map(item => {
        if (item.workspace_id !== owner.workspaceId) throw new Error('Thread 名称查询改变了工作区归属');
        const target: ThreadOwner = { ...owner, routeId: '', sessionId: item.session_id, threadId: item.thread_id, title: item.title };
        return { kind: 'resource', resource: new ThreadInput(target).resource, title: item.title, description: `${label} · ${item.session_title} · ${item.thread_id}`,
          detail: `${item.kind} Thread · ${item.session_id} / ${item.thread_id}` };
      }) };
  }
  const roots = await gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots',
    { signal, headers: gatewayWorkspaceRequestHeaders(target) }, target.workspace_id);
  const context = new SearchWorkspaceContext({ version: 1, workspaces: [{ ...owner, rootUri: roots.workspace_uri, label }] });
  try {
    const child = context.ownChild(instantiation.createChild(new ServiceCollection([IWorkspaceContextService, context])));
    const builder = child.createInstance(QueryBuilder);
    const complete = await search.fileSearchPage(builder.file(context.getWorkspace().folders, { filePattern: filter, maxResults: 50 }), cursor, token, deadlineAt);
    return { cursor: complete.cursor, requestIds: complete.requestIds ?? [],
      limited: complete.messages.length ? complete.messages.map(message => message.text).join('；')
        : complete.limitHit && !complete.cursor ? '文件结果达到本次上限，请缩小查询' : undefined,
      items: complete.results.map(item => {
        const relative = item.resource.path.split('/').slice(3);
        return { kind: 'resource', resource: item.resource, title: relative.at(-1)!,
          description: `${label} · ${relative.slice(0, -1).join('/')}`, detail: relative.join('/') };
      }) };
  } finally { context.dispose(); }
}
