import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { QueryBuilder } from '../../upstream/src/vs/workbench/services/search/common/queryBuilder.js';
import type { WorkspaceSearchService } from './searchService.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { TurnMessageMatchDTO, WorkspaceFileRootsDTO, LLMRequestLogMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gatewayWorkspaceRequestHeaders } from '../../../web/src/api/http.js';
import { gateway, type WorkspaceTarget } from '../agentApi.js';
import { SearchWorkspaceContext } from './scope.js';

export type NameSearchCategory = '会话' | 'Thread' | '文件';
export type ContentSearchCategory = '会话消息' | '工具结果' | 'LLM 请求记录';
export type SearchCategory = NameSearchCategory | ContentSearchCategory;
export type ResourceSearchItem = { title: string; description: string; detail: string } & (
  { kind: 'resource'; resource: URI; message?: TurnMessageMatchDTO } | { kind: 'session'; sessionId: string; requestLog?: LLMRequestLogMatchDTO } | { kind: 'folder'; nodeId: string }
);
export interface ResourceSearchPage {
  items: ResourceSearchItem[];
  cursor?: string;
  limited?: string;
  requestIds: string[];
}

/** 查询只读取所选工作区原 owner；不使用 Gateway 全范围聚合或前端全量目录。 */
export async function searchQuickAccessFiles(filter: string,
  owner: WorkspaceOwner, target: WorkspaceTarget, label: string, signal: AbortSignal, token: CancellationToken,
  instantiation: IInstantiationService, search: WorkspaceSearchService, cursor?: string, deadlineAt?: number): Promise<ResourceSearchPage> {
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
