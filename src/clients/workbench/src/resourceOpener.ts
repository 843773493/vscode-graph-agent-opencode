import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { IOpenerService, type IOpener, type OpenOptions } from '../upstream/src/vs/platform/opener/common/opener.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService, preferredSideBySideGroupDirection } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { GatewayResourceAddressDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v1/public.js';
import { gateway } from './agentApi.js';
import { ThreadInput } from './threadInput.js';
import { workspaceFileUri } from './workspaceFiles.js';
import { workspaceContext } from './workspaceContext.js';

export interface ResourceOpenOptions extends OpenOptions {
  boxteamTargetGroup?: number;
}

/** 所有规范资源链接共用原 Opener；模板和归属仍由后端唯一 owner 解释。 */
export class ResourceOpener extends Disposable implements IOpener {
  constructor(
    @IOpenerService opener: IOpenerService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IConfigurationService private readonly configuration: IConfigurationService,
  ) {
    super();
    this._register(opener.registerOpener(this));
  }

  async open(resource: URI | string, options: ResourceOpenOptions = {}): Promise<boolean> {
    const uri = typeof resource === 'string' ? resource : resource.toString();
    if (!/^boxteam:/i.test(uri)) return false;
    const groupId = options.boxteamTargetGroup ?? this.groups.activeGroup.id;
    const gatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!gatewayId) throw new Error('尚未取得发起 Gateway 身份，无法打开资源');
    const address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
      method: 'POST', body: JSON.stringify({ uri, local_gateway_id: gatewayId }),
    });
    if (workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('发起打开的 Gateway 身份已改变，请重新打开资源');
    if (address.scope !== 'workspace') throw new Error(`此资源尚未接入编辑器：${address.scope}/${address.kind}`);
    const owner = { gatewayId: address.gateway_id, workspaceId: address.scope_id };
    const target = await gateway.resolveWorkspaceTarget(owner);
    if (target.status !== 'ready') throw new Error(`目标工作区当前不可用（${target.status}），请明确启动或连接`);
    const input = address.session_id
      ? new ThreadInput({ ...owner, routeId: target.workspace_id, sessionId: address.session_id,
        threadId: address.thread_id ?? (await gateway.mainThread(target.workspace_id, address.session_id)).thread_id,
        title: address.thread_id ?? address.session_id })
      : address.file_root_uri ? { resource: workspaceFileUri(owner, address.uri, address.file_root_uri) } : undefined;
    // TODO：只读来源、媒体及更改的统一投影继续由同一入口补齐，不走系统外部打开。
    if (!input) throw new Error(`此资源尚未接入编辑器：${address.kind}`);
    const source = this.groups.getGroup(groupId);
    if (!source) throw new Error('发起打开的编辑器组已关闭');
    const direction = preferredSideBySideGroupDirection(this.configuration);
    const group = options.openToSide ? this.groups.findGroup({ direction }, source) ?? this.groups.addGroup(source, direction) : source;
    if (input instanceof ThreadInput) await this.editors.openEditor(input, options.editorOptions, group.id);
    else await this.editors.openEditor({ ...input, options: options.editorOptions }, group.id);
    return true;
  }
}

registerWorkbenchContribution2('boxteam.resourceOpener', ResourceOpener, WorkbenchPhase.BlockRestore);
