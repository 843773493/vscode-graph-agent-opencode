import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { IClipboardService } from '../upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { onGatewayUserSessionInvalidated } from '../../web/src/api/http.js';
import type { GatewayResourceAddressDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { sourceDocumentSource } from './sourceDocuments.js';
import { workspaceContext } from './workspaceContext.js';
import { workspaceFileSource } from './workspaceFiles.js';

export const COPY_RESOURCE_URI = 'boxteam.resource.copyUri';
export const ATTACH_RESOURCE_URI = 'boxteam.resource.attachToChat';
export const PASTE_SELECTED_CONTENT = 'boxteam.resource.pasteSelectionToChat';

export function supportsResourceReference(resource: URI): boolean {
  return resource.scheme === 'boxteam-file' || resource.scheme === 'boxteam-source';
}

/** 只反转内部编辑器投影；公开引用由统一寻址 owner 生成，不读取正文或打开资源。 */
export async function resourceReference(resource: URI): Promise<string> {
  const source = resource.scheme === 'boxteam-file' ? workspaceFileSource(resource)
    : resource.scheme === 'boxteam-source' ? sourceDocumentSource(resource) : undefined;
  if (!source) throw new Error('此资源尚未提供规范 URI 复制能力');
  const gatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!gatewayId) throw new Error('尚未取得发起 Gateway 身份，无法复制资源引用');
  let invalidated = false;
  const release = onGatewayUserSessionInvalidated(0, () => { invalidated = true; });
  try {
    const address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
      method: 'POST', body: JSON.stringify({ uri: 'path' in source ? source.path : source.uri,
        local_gateway_id: gatewayId, resource_gateway_id: source.gatewayId }),
    });
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('Gateway 或用户身份已改变，请重新复制资源引用');
    const sameScope = address.scope === 'workspace' ? address.scope_id === source.workspaceId
      : resource.scheme === 'boxteam-source' && address.scope === 'gateway' && address.kind === 'skills' && address.scope_id === source.gatewayId;
    if (address.gateway_id !== source.gatewayId || !sameScope) {
      throw new Error('资源引用的 Gateway／工作区归属不匹配');
    }
    return address.reference_uri;
  } finally { release(); }
}

const referenceContext = ContextKeyExpr.or(ContextKeyExpr.equals('resourceScheme', 'boxteam-file'), ContextKeyExpr.equals('resourceScheme', 'boxteam-source'));

registerAction2(class CopyResourceUri extends Action2 {
  constructor() {
    super({ id: COPY_RESOURCE_URI, title: { value: '复制资源 URI', original: 'Copy Resource URI' },
      f1: true, precondition: referenceContext,
      menu: { id: MenuId.EditorTitleContext, group: '2_copy', when: referenceContext } });
  }
  async run(accessor: ServicesAccessor, supplied?: URI): Promise<void> {
    if (supplied !== undefined && !URI.isUri(supplied)) throw new Error('复制资源引用需要明确的编辑器 URI');
    const resource = supplied ?? EditorResourceAccessor.getCanonicalUri(accessor.get(IEditorService).activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (!resource) throw new Error('没有明确的资源引用目标');
    const clipboard = accessor.get(IClipboardService);
    await clipboard.writeText(await resourceReference(resource));
  }
});
