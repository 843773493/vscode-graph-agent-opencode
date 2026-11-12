import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { IClipboardService } from '../upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import type { GatewayChangeAddressRequest, GatewayResourceAddressDTO, GatewayThreadAddressRequest } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { sourceDocumentSource } from './sourceDocuments.js';
import { workspaceContext } from './workspaceContext.js';
import { workspaceFileSource } from './workspaceFiles.js';
import { ThreadInput } from './threadInput.js';
import { configSourceScheme, sourceLocation } from './settings/sourceEditor.js';
import { ConfigurationSourceOwner } from './settings/sourceOwner.js';
import { changeFileSource } from './readonlyDocuments.js';

export const COPY_RESOURCE_URI = 'boxteam.resource.copyUri';
export const ATTACH_RESOURCE_URI = 'boxteam.resource.attachToChat';
export const PASTE_SELECTED_CONTENT = 'boxteam.resource.pasteSelectionToChat';
export const ATTACH_SOURCE_URIS = 'boxteam.resource.selectSourcesForChat';
export type ResourceReferenceOwner = { gatewayId: string; workspaceId?: string };

export function supportsResourceReference(resource: URI): boolean {
  return resource.scheme === 'boxteam-file' || resource.scheme === 'boxteam-source' || resource.scheme === 'boxteam-thread' || resource.scheme === 'boxteam-change' || resource.scheme === configSourceScheme;
}

export function supportsChatResourceReference(resource: URI): boolean {
  return resource.scheme !== 'boxteam-change' && supportsResourceReference(resource);
}

/** 只反转内部编辑器投影；公开引用由统一寻址 owner 生成，不读取正文或打开资源。 */
export async function resourceReference(resource: URI, resourceOwner?: ResourceReferenceOwner, usage: 'copy' | 'chat' = 'copy'): Promise<string> {
  const configuration = resource.scheme === configSourceScheme ? sourceLocation(resource) : undefined;
  const thread = resource.scheme === 'boxteam-thread' ? ThreadInput.fromResource(resource).owner : undefined;
  const change = resource.scheme === 'boxteam-change' ? changeFileSource(resource) : undefined;
  const source = resource.scheme === 'boxteam' && resourceOwner ? { ...resourceOwner, uri: resource.toString() }
    : resource.scheme === 'boxteam-file' ? workspaceFileSource(resource)
    : resource.scheme === 'boxteam-source' ? sourceDocumentSource(resource) : undefined;
  const owner = thread ?? change?.owner ?? source ?? (configuration && { gatewayId: configuration.target.gateway_id, workspaceId: configuration.target.workspace_id });
  if (!owner) throw new Error('此资源尚未提供规范 URI 复制能力');
  let sourceUri = source && ('path' in source ? source.path : source.uri);
  const gatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!gatewayId) throw new Error('尚未取得发起 Gateway 身份，无法复制资源引用');
  let invalidated = false;
  const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const assertAccess = () => {
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('Gateway 或访问凭据已改变，请重新复制资源引用');
  };
  try {
    if (configuration) {
      // 已打开的配置只重读原目录元数据；不从来源名造地址，也不读取草稿或配置正文。
      const listed = await new ConfigurationSourceOwner(configuration.target).list();
      assertAccess();
      const entry = listed.sources.find(item => item.source_key === configuration.sourceKey);
      if (!entry?.uri) throw new Error('此配置来源没有可复制的规范 URI，请使用原设置来源入口');
      sourceUri = entry.uri;
    }
    const address = await gateway.request<GatewayResourceAddressDTO>(change ? '/api/gateway/resource-addresses/change' : thread
      ? '/api/gateway/resource-addresses/thread' : '/api/gateway/resource-addresses/parse', {
      method: 'POST', body: JSON.stringify(change ? {
        local_gateway_id: gatewayId, resource_gateway_id: change.owner.gatewayId, workspace_id: change.owner.workspaceId,
        session_id: change.owner.sessionId, changeset_id: change.changesetId, file_uri: change.filePath,
      } satisfies GatewayChangeAddressRequest : thread ? {
        local_gateway_id: gatewayId, resource_gateway_id: thread.gatewayId,
        workspace_id: thread.workspaceId, session_id: thread.sessionId, thread_id: thread.threadId,
      } satisfies GatewayThreadAddressRequest : { uri: sourceUri,
        local_gateway_id: gatewayId, resource_gateway_id: owner.gatewayId }),
    });
    assertAccess();
    const sameScope = address.scope === 'workspace' ? address.scope_id === owner.workspaceId
      : (configuration || resource.scheme === 'boxteam') && address.kind === 'config'
        && (address.scope === 'inline' || address.scope === 'user' || address.scope === 'gateway' && address.scope_id === owner.gatewayId)
        || resource.scheme === 'boxteam-source' && address.scope === 'gateway' && address.kind === 'skills' && address.scope_id === owner.gatewayId;
    if (address.gateway_id !== owner.gatewayId || !sameScope) {
      throw new Error('资源引用的 Gateway／工作区归属不匹配');
    }
    if (thread && (address.kind !== 'session' || address.session_id !== thread.sessionId || address.thread_id !== thread.threadId)) {
      throw new Error('资源引用不属于指定的 Session／Thread');
    }
    if (configuration && address.kind !== 'config') throw new Error('配置来源没有返回规范配置资源 URI');
    if (change && (address.kind !== 'changes' || address.session_id !== change.owner.sessionId || address.changeset_id !== change.changesetId)) {
      throw new Error('更改引用不属于指定的 Session／变更范围');
    }
    if (usage === 'chat' && address.kind === 'changes') throw new Error('更改 URI 用于打开 Diff；请使用选定正文粘贴到聊天草稿');
    return address.reference_uri;
  } finally { release(); }
}

const referenceContext = ContextKeyExpr.or(...['boxteam-file', 'boxteam-source', 'boxteam-thread', 'boxteam-change', configSourceScheme].map(scheme => ContextKeyExpr.equals('resourceScheme', scheme)));

registerAction2(class CopyResourceUri extends Action2 {
  constructor() {
    super({ id: COPY_RESOURCE_URI, title: { value: '复制资源 URI', original: 'Copy Resource URI' },
      f1: true, precondition: referenceContext,
      menu: { id: MenuId.EditorTitleContext, group: '2_copy', when: referenceContext } });
  }
  async run(accessor: ServicesAccessor, supplied?: URI, owner?: ResourceReferenceOwner): Promise<void> {
    if (supplied !== undefined && !URI.isUri(supplied)) throw new Error('复制资源引用需要明确的编辑器 URI');
    const resource = supplied ?? EditorResourceAccessor.getCanonicalUri(accessor.get(IEditorService).activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (!resource) throw new Error('没有明确的资源引用目标');
    const clipboard = accessor.get(IClipboardService);
    await clipboard.writeText(await resourceReference(resource, owner));
  }
});
