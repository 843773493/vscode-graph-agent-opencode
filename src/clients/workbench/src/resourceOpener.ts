import { gatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { Disposable, type IReference } from '../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { ITextModelService, type IResolvedTextEditorModel } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { IOpenerService, type IOpener, type OpenOptions } from '../upstream/src/vs/platform/opener/common/opener.js';
import { getWorkbenchContribution, registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import type { IResourceDiffEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import type { IResourceEditorInput, ITextEditorOptions } from '../upstream/src/vs/platform/editor/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService, preferredSideBySideGroupDirection } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IEditorResolverService } from '../upstream/src/vs/workbench/services/editor/common/editorResolverService.js';
import type { GatewayResourceAddressDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { ThreadInput } from './threadInput.js';
import { workspaceFileEditorInput, workspaceFileUri } from './workspaceFiles.js';
import { workspaceContext } from './workspaceContext.js';
import { sourceDocumentUri } from './sourceDocuments.js';
import { ChangeDocuments } from './changeFiles.js';
import { changeFileUri } from './readonlyDocuments.js';
import { captureSearchOwner } from './search/target.js';
import { ConfigurationSourceOwner, type SourceTarget } from './settings/sourceOwner.js';
import { openConfigurationSourceCommand, type ConfigurationSourceOpenRequest } from './settings/commands.js';
import type { ThreadEditorOptions } from './threadPane.js';
import { t } from '../../shared/core/i18n/index.js';

export interface ResourceOpenOptions extends OpenOptions {
  boxteamTargetGroup?: number;
  boxteamResourceOwner?: WorkspaceOwner;
  boxteamResourceLabel?: string;
  editorOptions?: ThreadEditorOptions & ITextEditorOptions;
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
    @IEditorResolverService private readonly resolver: IEditorResolverService,
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
    const label = options.boxteamResourceLabel;
    const resourceOwner = options.boxteamResourceOwner && { ...options.boxteamResourceOwner };
    const gatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!gatewayId) throw new Error(t('quickAccess.resource.gatewayIdentityMissing'));
    // inline 配置本身不含业务目标；在异步解析前捕获发起者，不从 URI 文件名猜配置域。
    let sourceOwner = resourceOwner;
    if (!sourceOwner) {
      try { sourceOwner = captureSearchOwner(this.editors); } catch { /* 目标自包含的 URI 不需要发起编辑器归属。 */ }
    }
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    const assertAccess = () => {
      if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) {
        throw new Error(t('quickAccess.resource.identityChanged'));
      }
    };
    const references: IReference<IResolvedTextEditorModel>[] = [];
    try {
      let address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
        method: 'POST', body: JSON.stringify({ uri, local_gateway_id: gatewayId, resource_gateway_id: resourceOwner?.gatewayId }),
      });
      assertAccess();
      if (address.kind === 'config' && address.gateway_id !== gatewayId && !resourceOwner) {
        // 远端 reference_uri 再交同一 grammar，以目标 Gateway 规范化 owner URI。
        address = await gateway.request<GatewayResourceAddressDTO>('/api/gateway/resource-addresses/parse', {
          method: 'POST', body: JSON.stringify({ uri: address.reference_uri, local_gateway_id: gatewayId, resource_gateway_id: address.gateway_id }),
        });
        assertAccess();
      }
      if (address.kind === 'config') {
        const targets: SourceTarget[] = address.scope === 'workspace'
          ? [{ gateway_id: address.gateway_id, workspace_id: address.scope_id }]
          : [{ gateway_id: address.gateway_id }, ...(address.scope !== 'gateway' && sourceOwner?.gatewayId === address.gateway_id
            ? [{ gateway_id: sourceOwner.gatewayId, workspace_id: sourceOwner.workspaceId }] : [])];
        if (resourceOwner && (address.gateway_id !== resourceOwner.gatewayId
          || address.scope === 'workspace' && address.scope_id !== resourceOwner.workspaceId)) {
          throw new Error(t('quickAccess.resource.configOwnerMismatch'));
        }
        const matches = (await Promise.all(targets.map(async target => {
          const listed = await new ConfigurationSourceOwner(target).list();
          return listed.sources.flatMap(source => source.uri === address.uri && source.source_key
            ? [{ target, sourceKey: source.source_key }] : []);
        }))).flat();
        assertAccess();
        if (matches.length !== 1) throw new Error(t(matches.length ? 'quickAccess.resource.configMultipleSources' : 'quickAccess.resource.configSourceMissing'));
        const match = matches[0];
        await this.commands.executeCommand(openConfigurationSourceCommand, {
          location: { ...match.target, version: 2, source_key: match.sourceKey, name: match.target.workspace_id ?? 'Gateway' },
          groupId, openToSide, editorOptions,
        } satisfies ConfigurationSourceOpenRequest);
        return true;
      }
      let owner: WorkspaceOwner;
      if (address.scope === 'workspace') {
        owner = { gatewayId: address.gateway_id, workspaceId: address.scope_id };
      } else if (address.scope === 'gateway' && address.kind === 'skills') {
        // Gateway 来源仍有 Workspace audience；沿发现入口固定的工作区读取，不能猜当前激活工作区。
        if (!resourceOwner || address.scope_id !== address.gateway_id) throw new Error(t('quickAccess.resource.skillOwnerRequired'));
        owner = resourceOwner;
      } else {
        throw new Error(t('quickAccess.resource.unsupportedEditor', address.scope, address.kind));
      }
      if (resourceOwner && (address.gateway_id !== resourceOwner.gatewayId || owner.workspaceId !== resourceOwner.workspaceId)) {
        throw new Error(t('quickAccess.resource.ownerMismatch'));
      }
      let input: ThreadInput | IResourceEditorInput | IResourceDiffEditorInput;
      if (address.kind === 'changes') {
        if (!address.session_id || !address.changeset_id || !address.change_file_uri) throw new Error(t('quickAccess.resource.changeReferenceMissing'));
        const changeOwner = { ...owner, sessionId: address.session_id };
        const changesetId = address.changeset_id;
        input = await getWorkbenchContribution<ChangeDocuments>(ChangeDocuments.ID).withFile({ owner: changeOwner,
          changesetId, filePath: address.change_file_uri }, async file => {
          assertAccess();
          const original = { resource: changeFileUri(changeOwner, changesetId, file, 'before') };
          const modified = { resource: changeFileUri(changeOwner, changesetId, file, 'after') };
          references.push(await this.models.createModelReference(original.resource));
          assertAccess();
          references.push(await this.models.createModelReference(modified.resource));
          return { original, modified, label: file.file_path };
        });
      } else if (address.session_id || address.file_root_uri) {
        const trace = editorOptions?.boxteamTrace?.reference, requestLog = editorOptions?.boxteamRequestLog?.reference;
        if (trace && (trace.session_id !== address.session_id || address.thread_id && trace.thread_id !== address.thread_id)
          || requestLog && requestLog.session_id !== address.session_id) {
          throw new Error(t('quickAccess.resource.logLocationMismatch'));
        }
        const target = await gateway.resolveWorkspaceTarget(owner);
        assertAccess();
        if (target.status !== 'ready') throw new Error(t('quickAccess.resource.workspaceUnavailable', target.status));
        input = address.session_id
          ? new ThreadInput({ ...owner, workspaceKey: gatewayWorkspaceKey(target), sessionId: address.session_id,
            threadId: address.thread_id ?? trace?.thread_id ?? (await gateway.mainThread(gatewayWorkspaceKey(target), address.session_id)).thread_id,
            title: label ?? address.thread_id ?? address.session_id })
          : { resource: workspaceFileUri(owner, address.uri, address.file_root_uri!) };
      } else {
        // 单文件来源（包括 Session 产物）交原导航 owner 核验读取能力，不在客户端重复 URI 模板。
        const sourceUri = address.gateway_id === gatewayId ? address.uri : address.reference_uri;
        input = { resource: sourceDocumentUri(owner, sourceUri), description: `${owner.workspaceId} · ${address.kind}` };
        // 沿原 resolver 先取得引用：一次重解路由及读取即可完成核验，失败时不创建空分屏。
        references.push(await this.models.createModelReference(input.resource));
      }
      if (!(input instanceof ThreadInput)) input = await workspaceFileEditorInput({ ...input, options: editorOptions }, this.resolver);
      assertAccess();
      const source = this.groups.getGroup(groupId);
      if (!source) throw new Error(t('quickAccess.resource.invokingGroupClosed'));
      const direction = preferredSideBySideGroupDirection(this.configuration);
      const group = openToSide ? this.groups.findGroup({ direction }, source) ?? this.groups.addGroup(source, direction) : source;
      if (input instanceof ThreadInput) await this.editors.openEditor(input, editorOptions, group.id);
      else await this.editors.openEditor(input, group.id);
      return true;
    } finally { for (const reference of references) reference.dispose(); release(); }
  }
}

registerWorkbenchContribution2('boxteam.resourceOpener', ResourceOpener, WorkbenchPhase.BlockRestore);
