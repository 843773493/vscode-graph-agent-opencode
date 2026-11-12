import { t } from '../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { basename } from '../upstream/src/vs/base/common/resources.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { ILanguageService } from '../upstream/src/vs/editor/common/languages/language.js';
import { IModelService } from '../upstream/src/vs/editor/common/services/model.js';
import { ITextModelService, type ITextModelContentProvider } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import type { ResourceNavigationResultDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from './agentApi.js';

const scheme = 'boxteam-source';

/** 仅为原版编辑器投影身份；规范 URI 作为不透明引用，仍由后端唯一 grammar 解释。 */
export function sourceDocumentUri(owner: WorkspaceOwner, uri: string): URI {
  return URI.from({ scheme, authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(uri)}/${basename(URI.parse(uri))}` });
}

export function sourceDocumentSource(resource: URI): WorkspaceOwner & { uri: string } {
  const [, workspaceId, reference, name, ...extra] = resource.path.split('/');
  if (resource.scheme !== scheme || !resource.authority || !workspaceId || !reference || !name || extra.length || resource.query || resource.fragment) {
    throw new Error(t('workbench.source.identityMissing'));
  }
  return { gatewayId: resource.authority, workspaceId, uri: decodeURIComponent(reference) };
}

/** 无 FileService writer：原 TextResourceEditorModel 负责只读、引用计数及释放。 */
export class SourceDocuments extends Disposable implements ITextModelContentProvider {
  constructor(
    @ITextModelService resolver: ITextModelService,
    @IModelService private readonly models: IModelService,
    @ILanguageService private readonly languages: ILanguageService,
  ) {
    super();
    this._register(resolver.registerTextModelContentProvider(scheme, this));
  }

  async provideTextContent(resource: URI) {
    const existing = this.models.getModel(resource);
    if (existing) return existing;
    const source = sourceDocumentSource(resource);
    const target = await gateway.resolveWorkspaceTarget(source);
    if (target.status !== 'ready') throw new Error(t('workbench.source.workspaceUnavailable', target.status));
    const result = await gateway.request<ResourceNavigationResultDTO>('/api/v1/resources/navigation', {
      method: 'POST', body: JSON.stringify({ uri: source.uri, operation: 'read', read_format: 'auto',
        max_bytes: 33554432, max_response_bytes: 50331648 }),
    }, gatewayWorkspaceKey(target));
    if (!result.node || result.node.uri !== source.uri || result.node.resource_shape !== 'file') throw new Error(t('workbench.source.responseMismatch'));
    if (result.truncated) throw new Error(t('workbench.source.truncated', result.truncation_reason));
    if (result.media_type !== 'text' || result.content_encoding !== 'utf-8' || result.content == null) {
      throw new Error(t('workbench.source.textRequired'));
    }
    // resolver 合并同一资源的并发引用；语言只依据展示文件名，不影响业务定位。
    return this.models.createModel(result.content, this.languages.createByFilepathOrFirstLine(resource), resource);
  }
}

registerWorkbenchContribution2('boxteam.sourceDocuments', SourceDocuments, WorkbenchPhase.BlockRestore);
