import { gatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { Disposable, type IReference } from '../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { ITextModelService, type IResolvedTextEditorModel } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { IOpenerService, type IOpener, type OpenOptions } from '../upstream/src/vs/platform/opener/common/opener.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import type { IResourceEditorInput } from '../upstream/src/vs/platform/editor/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService, preferredSideBySideGroupDirection } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { GatewayResourceAddressDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { ThreadInput } from './threadInput.js';
import { workspaceFileUri } from './workspaceFiles.js';
import { workspaceContext } from './workspaceContext.js';
import { sourceDocumentUri } from './sourceDocuments.js';
import { captureSearchOwner } from './search/target.js';
import { ConfigurationSourceOwner, type SourceTarget } from './settings/sourceOwner.js';
import { openConfigurationSourceCommand, type ConfigurationSourceOpenRequest } from './settings/commands.js';

export interface ResourceOpenOptions extends OpenOptions {
  boxteamTargetGroup?: number;
  boxteamResourceOwner?: WorkspaceOwner;
}

/** 所有规范资源链接共用原 Opener；模板和归属仍由后端唯一 owner 解释。 */
export class ResourceOpener extends Disposable implements IOpener {
  constructor(
    @IOpenerService opener: IOpenerService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @ITextModelService private readonly models: ITextModelService,
    @ICommandService private readonly commands: ICommandService,
  ) {
    super();
    this._register(opener.registerOpener(this));
  }

  async open(resource: URI | string, options: ResourceOpenOptions = {}): Promise<boolean> {
    const uri = typeof resource === 'string' ? resource : resource.toString();
    if (!/^boxteam:/i.test(uri)) return false;
    const groupId = options.boxteamTargetGroup ?? this.groups.activeGroup.id;
    const openToSide = options.openToSide;
    const editorOptions = options.editorOptions && { ...options.editorOptions };
    const resourceOwner = options.boxteamResourceOwner && { ...options.boxteamResourceOwner };
    const gatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!gatewayId) throw new Error('尚未取得发起 Gateway 身份，无法打开资源');
    // inline 配置本身不含业务目标；在异步解析前捕获发起者，不从 URI 文件名猜配置域。
    let sourceOwner = resourceOwner;
    if (!sourceOwner) {
      try { sourceOwner = captureSearchOwner(this.editors); } catch { /* 目标自包含的 URI 不需要发起编辑器归属。 */ }
    }
    let invalidatedDuringParse = false;
    const releaseParse = onGatewayTokenInvalidated(0, () => { invalidatedDuringParse = true; });
    let address: GatewayResourceAddressDTO;
    try {
      address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
        method: 'POST', body: JSON.stringify({ uri, local_gateway_id: gatewayId, resource_gateway_id: resourceOwner?.gatewayId }),
      });
      if (address.kind === 'config' && address.gateway_id !== gatewayId && !resourceOwner) {
        // 远端 reference_uri 再交同一 grammar，以目标 Gateway 规范化 owner URI。
        address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
          method: 'POST', body: JSON.stringify({ uri: address.reference_uri, local_gateway_id: gatewayId, resource_gateway_id: address.gateway_id }),
        });
      }
    } finally { releaseParse(); }
    if (workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('发起打开的 Gateway 身份已改变，请重新打开资源');
    if (address.kind === 'config') {
      if (invalidatedDuringParse) throw new Error('Gateway 或访问凭据已改变，请重新打开配置来源');
      let invalidated = false;
      const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
      try {
        const targets: SourceTarget[] = address.scope === 'workspace'
          ? [{ gateway_id: address.gateway_id, workspace_id: address.scope_id }]
          : [{ gateway_id: address.gateway_id }, ...(address.scope !== 'gateway' && sourceOwner?.gatewayId === address.gateway_id
            ? [{ gateway_id: sourceOwner.gatewayId, workspace_id: sourceOwner.workspaceId }] : [])];
        if (resourceOwner && (address.gateway_id !== resourceOwner.gatewayId
          || address.scope === 'workspace' && address.scope_id !== resourceOwner.workspaceId)) {
          throw new Error('配置 URI 与明确的 Gateway／工作区归属不匹配');
        }
        const matches = (await Promise.all(targets.map(async target => {
          const listed = await new ConfigurationSourceOwner(target).list();
          return listed.sources.flatMap(source => source.uri === address.uri && source.source_key
            ? [{ target, sourceKey: source.source_key }] : []);
        }))).flat();
        if (matches.length !== 1) throw new Error(matches.length ? '配置 URI 对应多个正式来源，请明确选择原来源' : '目标配置 owner 没有公布此 URI 对应的来源');
        if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('Gateway 或访问凭据已改变，请重新打开配置来源');
        const match = matches[0];
        await this.commands.executeCommand(openConfigurationSourceCommand, {
          location: { ...match.target, version: 2, source_key: match.sourceKey, name: match.target.workspace_id ?? 'Gateway' },
          groupId, openToSide, editorOptions,
        } satisfies ConfigurationSourceOpenRequest);
        return true;
      } finally { release(); }
    }
    let owner: WorkspaceOwner;
    if (address.scope === 'workspace') {
      owner = { gatewayId: address.gateway_id, workspaceId: address.scope_id };
    } else if (address.scope === 'gateway' && address.kind === 'skills') {
      // Gateway 来源仍有 Workspace audience；沿发现入口固定的工作区读取，不能猜当前激活工作区。
      if (!resourceOwner || address.scope_id !== address.gateway_id) throw new Error('Gateway Skill 打开需要明确的所属 Gateway 和读取工作区');
      owner = resourceOwner;
    } else {
      throw new Error(`此资源尚未接入编辑器：${address.scope}/${address.kind}`);
    }
    if (resourceOwner && (address.gateway_id !== resourceOwner.gatewayId || owner.workspaceId !== resourceOwner.workspaceId)) {
      throw new Error('资源解析响应与明确的 Gateway／工作区归属不匹配');
    }
    let input: ThreadInput | IResourceEditorInput;
    let reference: IReference<IResolvedTextEditorModel> | undefined;
    if (address.session_id || address.file_root_uri) {
      const target = await gateway.resolveWorkspaceTarget(owner);
      if (target.status !== 'ready') throw new Error(`目标工作区当前不可用（${target.status}），请明确启动或连接`);
      input = address.session_id
        ? new ThreadInput({ ...owner, workspaceKey: gatewayWorkspaceKey(target), sessionId: address.session_id,
          threadId: address.thread_id ?? (await gateway.mainThread(gatewayWorkspaceKey(target), address.session_id)).thread_id,
          title: address.thread_id ?? address.session_id })
        : { resource: workspaceFileUri(owner, address.uri, address.file_root_uri!) };
    } else {
      // 单文件来源（包括 Session 产物）交原导航 owner 核验读取能力，不在客户端重复 URI 模板。
      const sourceUri = address.gateway_id === gatewayId ? address.uri : address.reference_uri;
      input = { resource: sourceDocumentUri(owner, sourceUri), description: `${owner.workspaceId} · ${address.kind}` };
      // 沿原 resolver 先取得引用：一次重解路由及读取即可完成核验，失败时不创建空分屏。
      reference = await this.models.createModelReference(input.resource);
    }
    try {
      const source = this.groups.getGroup(groupId);
      if (!source) throw new Error('发起打开的编辑器组已关闭');
      const direction = preferredSideBySideGroupDirection(this.configuration);
      const group = openToSide ? this.groups.findGroup({ direction }, source) ?? this.groups.addGroup(source, direction) : source;
      if (input instanceof ThreadInput) await this.editors.openEditor(input, editorOptions, group.id);
      else await this.editors.openEditor({ ...input, options: editorOptions }, group.id);
      return true;
    } finally { reference?.dispose(); }
  }
}

registerWorkbenchContribution2('boxteam.resourceOpener', ResourceOpener, WorkbenchPhase.BlockRestore);
