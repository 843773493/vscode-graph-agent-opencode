import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Event } from '../../upstream/src/vs/base/common/event.js';
import { DisposableStore, IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import { ConfigurationTarget } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { SyncDescriptor } from '../../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { PreferencesSearchService } from '../../upstream/src/vs/workbench/contrib/preferences/browser/preferencesSearch.js';
import { IPreferencesSearchService } from '../../upstream/src/vs/workbench/contrib/preferences/common/preferences.js';
import { PreferencesService } from '../../upstream/src/vs/workbench/services/preferences/browser/preferencesService.js';
import { IPreferencesService } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import type { ISettingsEditorOptions } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { ITextFileService } from '../../upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import type { ITextFileEditorModel } from '../../upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import { extUri, joinPath } from '../../upstream/src/vs/base/common/resources.js';
import type { ExtensionConfigurationTargetRequest, ExtensionConfigurationViewDTO, ExtensionConfigurationTargetListDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from '../agentApi.js';
import { targetConfigurationServices } from '../extensions/configuration.js';
import { registerTargetConfigurationFiles } from '../extensions/configurationFiles.js';
import { TargetWorkspaceResources } from '../extensions/targetUris.js';
import type { TargetConfigurationContext } from '../extensions/targetContext.js';
import { targetSettings, targetWorkspace } from '../extensions/targetContext.js';
import { managementGateway } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { ITargetSettingsContext, type TargetSettingsLocation } from './scope.js';

export function settingsEditorIdentity(location: TargetSettingsLocation): string {
  return JSON.stringify([location.gateway_id, location.backend_id, location.profile_id, location.view_id, location.workspace_ids]);
}

/** 历史只保存稳定归属；恢复交原 Gateway registry 解析当前唯一连接，歧义仍明确拒绝。 */
export function settingsHistoryLocation(location: TargetSettingsLocation): Omit<TargetSettingsLocation, 'gateway_connection_id'> {
  const { gateway_connection_id: _route, ...stable } = location;
  return stable;
}

/** 活跃输入保留明确连接；持久化边界另行投影稳定归属。 */
export function parseSettingsLocation(value: unknown): TargetSettingsLocation {
  if (!value || typeof value !== 'object') throw new Error('设置历史缺少明确目标');
  const location = value as Record<string, unknown>;
  if (location.version !== 1 || !['gateway_id', 'backend_id', 'profile_id', 'view_id', 'name'].every(key => typeof location[key] === 'string' && location[key])
    || !Array.isArray(location.workspace_ids) || !location.workspace_ids.every(id => typeof id === 'string' && id)
    || location.gateway_connection_id !== undefined && typeof location.gateway_connection_id !== 'string') throw new Error('设置历史目标格式无效');
  return { version: 1, gateway_id: location.gateway_id as string, backend_id: location.backend_id as string,
    profile_id: location.profile_id as string, view_id: location.view_id as string, name: location.name as string,
    workspace_ids: [...location.workspace_ids], gateway_connection_id: location.gateway_connection_id as string | undefined };
}

/** 同一 profile／folder JSON 使用文件本身的规范归属，不继承首次打开它的偶然 view。 */
export function settingsFileLocation(location: TargetSettingsLocation, context: TargetConfigurationContext, resource: URI): TargetSettingsLocation {
  if (extUri.isEqual(resource, URI.parse(targetSettings(context).resource_uri))) return { ...location, view_id: 'settings-profile', workspace_ids: [] };
  const workspace = targetWorkspace(context), resources = new TargetWorkspaceResources(context);
  const index = resources.folders.findIndex(folder => extUri.isEqual(joinPath(folder.uri, '.vscode/settings.json'), resource));
  return index < 0 ? location : { ...location, view_id: 'settings-folder', workspace_ids: [workspace.folders[index].workspace_id] };
}

export function settingsFileOptions(context: TargetConfigurationContext, resource: URI): ISettingsEditorOptions {
  if (extUri.isEqual(resource, URI.parse(targetSettings(context).resource_uri))) return { target: ConfigurationTarget.USER_REMOTE };
  const workspace = targetWorkspace(context);
  if (workspace.configuration && extUri.isEqual(resource, URI.parse(workspace.configuration.resource_uri))) return { target: ConfigurationTarget.WORKSPACE };
  const folder = new TargetWorkspaceResources(context).folders.find(folder => extUri.isEqual(joinPath(folder.uri, '.vscode/settings.json'), resource));
  if (!folder) throw new Error('文件不是当前目标的设置来源');
  return { target: ConfigurationTarget.WORKSPACE_FOLDER, folderUri: folder.uri };
}

/** 入口只冻结明确目标；不启动扩展宿主，不把焦点作为后续保存的路由。 */
export async function captureSettingsLocation(parent: IInstantiationService, token: CancellationToken): Promise<{
  location: TargetSettingsLocation; context: TargetConfigurationContext & { kind: 'settings' };
}> {
  const target = managementGateway('settings');
  const controller = new AbortController(), cancellation = token.onCancellationRequested(() => controller.abort());
  try {
    if (token.isCancellationRequested) throw new CancellationError();
    let backendId = target.backend_id;
    if (!backendId) {
      const directory = await gateway.request<ExtensionConfigurationTargetListDTO>(
        '/api/gateway/extension-configurations/targets', { method: 'POST', signal: controller.signal,
          body: JSON.stringify({ protocol_version: 1, gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id }) });
      controller.signal.throwIfAborted();
      if (directory.protocol_version !== 1 || directory.gateway_id !== target.gateway_id) throw new Error('配置目标目录不属于所选 Gateway');
      if (!directory.backend_ids.length) throw new Error('所选 Gateway 没有可用的配置目标，请显式启动 Backend 后重试');
      if (directory.backend_ids.length === 1) backendId = directory.backend_ids[0];
      else if (directory.backend_ids.length) {
        backendId = await parent.invokeFunction(accessor => accessor.get(IQuickInputService)).pick(directory.backend_ids.map(id => ({
          label: id, description: target.name,
        })), { title: '选择设置配置域', placeHolder: '配置将保存到这个 Backend 的默认 profile' }, token).then(value => value?.label);
      }
      if (!backendId) throw new CancellationError();
    }
    const roots = parent.invokeFunction(accessor => accessor.get(IWorkspaceContextService)).getWorkspace().folders;
    const workspaceIds = roots.filter(folder => folder.uri.scheme === 'boxteam-file').map(folder => workspaceFileSource(folder.uri))
      .filter(owner => owner.gatewayId === target.gateway_id).filter(owner => {
        const registered = workspaceContext.getSnapshot().workspaces.find(workspace => workspace.gateway_id === owner.gatewayId && workspace.workspace_id === owner.workspaceId);
        if (!registered?.backend_id) throw new Error('工作区尚无已确认的 Backend 身份，请重新读取原目标');
        return registered.backend_id === backendId;
      }).map(owner => owner.workspaceId);
    const request: ExtensionConfigurationTargetRequest = { protocol_version: 1, gateway_id: target.gateway_id,
      backend_id: backendId, view_id: 'default', workspace_ids: workspaceIds, gateway_connection_id: target.gateway_connection_id };
    const prepared = await prepareSettingsContext(request, controller.signal);
    return { location: { version: 1, gateway_id: target.gateway_id, backend_id: backendId, view_id: request.view_id,
      workspace_ids: workspaceIds, profile_id: prepared.connection.profile_id, name: target.name, gateway_connection_id: target.gateway_connection_id }, context: prepared };
  } finally { cancellation.dispose(); controller.abort(); }
}

export async function prepareSettingsContext(target: ExtensionConfigurationTargetRequest | TargetSettingsLocation, signal: AbortSignal): Promise<TargetConfigurationContext & { kind: 'settings' }> {
  const request: ExtensionConfigurationTargetRequest = { protocol_version: 1, gateway_id: target.gateway_id, backend_id: target.backend_id,
    view_id: target.view_id, workspace_ids: [...target.workspace_ids], gateway_connection_id: target.gateway_connection_id };
  const connection = await gateway.request<ExtensionConfigurationViewDTO>('/api/gateway/extension-configurations/prepare', {
    method: 'POST', body: JSON.stringify(request), signal,
  });
  signal.throwIfAborted();
  if (connection.protocol_version !== 1 || connection.gateway_id !== target.gateway_id || connection.backend_id !== target.backend_id
    || connection.view_id !== target.view_id || connection.workspace_ids.length !== target.workspace_ids.length
    || connection.workspace_ids.some((id, index) => id !== target.workspace_ids[index])
    || 'profile_id' in target && connection.profile_id !== target.profile_id) throw new Error('配置准备回执不属于原目标、profile 或有序视图');
  const context = { kind: 'settings' as const, connection, gatewayConnectionId: target.gateway_connection_id };
  new TargetWorkspaceResources(context);
  return context;
}

/** 每组在原 group child 上派生原服务，避免覆盖组的 context key 与进度服务。 */
export async function settingsEditorServices(parent: IInstantiationService, location: TargetSettingsLocation,
  context: TargetConfigurationContext, lifetime: DisposableStore): Promise<IInstantiationService> {
  const services = new ServiceCollection([ITargetSettingsContext, { _serviceBrand: undefined, target: { location, configuration: context } }],
    [IPreferencesService, new SyncDescriptor(PreferencesService)], [IPreferencesSearchService, new SyncDescriptor(PreferencesSearchService)]);
  return targetConfigurationServices(parent, context, lifetime, services);
}

export async function prepareSettingsPane(parent: IInstantiationService, location: TargetSettingsLocation,
  initialContext: TargetConfigurationContext | undefined, lifetime: DisposableStore, token: CancellationToken, resource?: URI): Promise<IInstantiationService> {
  const controller = new AbortController(), cancellation = token.onCancellationRequested(() => controller.abort());
  try {
    if (token.isCancellationRequested) throw new CancellationError();
    const context = initialContext ?? await prepareSettingsContext(location, controller.signal);
    if (resource) {
      const resources = new TargetWorkspaceResources(context);
      if (!resources.ownsConfigurationResource(resource) && !resources.folders.some(folder => extUri.isEqual(joinPath(folder.uri, '.vscode/settings.json'), resource))) {
        throw new Error('设置 JSON 资源不属于已冻结的 profile 或工作区视图');
      }
    }
    return await settingsEditorServices(parent, location, context, lifetime);
  } finally { cancellation.dispose(); controller.abort(); }
}

const retainedFileModels = new WeakSet<ITextFileEditorModel>();
/** JSON 文件模型持有独立传输引用，关闭设置面板不会中止原 TextFileService 的保存。 */
export async function retainSettingsFile<T>(parent: IInstantiationService, context: TargetConfigurationContext, resource: URI,
  open: () => Promise<T>): Promise<T> {
  const files = parent.invokeFunction(accessor => accessor.get(IFileService));
  const reference: IDisposable = registerTargetConfigurationFiles(files, context);
  let retained = false;
  try {
    const result = await open();
    const model = parent.invokeFunction(accessor => accessor.get(ITextFileService)).files.get(resource);
    if (model && !retainedFileModels.has(model)) {
      retainedFileModels.add(model);
      Event.once(model.onWillDispose)(() => { retainedFileModels.delete(model); reference.dispose(); });
      retained = true;
    }
    return result;
  } finally { if (!retained) reference.dispose(); }
}
