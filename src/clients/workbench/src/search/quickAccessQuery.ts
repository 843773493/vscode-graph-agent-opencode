import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { QueryBuilder } from '../../upstream/src/vs/workbench/services/search/common/queryBuilder.js';
import type { FederatedSearchQueryDTO } from '../../../web/src/types/protocol_generated/boxteam/search/v1/public.js';
import type { TurnMessageMatchDTO, LLMRequestLogMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { fileOptions, validateQuery } from './query.js';
import { SearchWorkspaceContext, type SearchScope } from './scope.js';

export type NameSearchCategory = '会话' | 'Thread' | '文件';
export type ContentSearchCategory = '会话消息' | '工具结果' | 'LLM 请求记录';
export type SearchCategory = NameSearchCategory | ContentSearchCategory;
export type ResourceSearchItem = { title: string; description: string; detail: string } & (
  { kind: 'resource'; resource: URI; message?: TurnMessageMatchDTO } | { kind: 'session'; sessionId: string; requestLog?: LLMRequestLogMatchDTO } | { kind: 'folder'; nodeId: string }
);

/** 原 QueryBuilder 继续解释配置；只构建根局部条件，远端分发统一交 Gateway。 */
export function quickAccessFileQuery(filter: string, scope: SearchScope, instantiation: IInstantiationService): FederatedSearchQueryDTO {
  const context = new SearchWorkspaceContext(scope);
  try {
    const child = context.ownChild(instantiation.createChild(new ServiceCollection([IWorkspaceContextService, context])));
    const builder = child.createInstance(QueryBuilder);
    const identity = child.invokeFunction(accessor => accessor.get(IUriIdentityService)).extUri;
    const query = validateQuery(builder.file(context.getWorkspace().folders, { filePattern: filter, maxResults: 50 }), identity);
    return { category: 'file', operation: 'name', contract_version: 1, query: query.filePattern || '**/*',
      match_mode: query.shouldGlobMatchFilePattern || !query.filePattern ? 'glob' : 'fuzzy', limit: 50,
      file_targets: query.folderQueries.map(folder => {
        const owner = workspaceFileSource(folder.folder);
        return { owner: { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId },
          target: { uri: owner.path, search_options: fileOptions(query, folder, identity), max_bytes: 65536 } };
      }) };
  } finally { context.dispose(); }
}
