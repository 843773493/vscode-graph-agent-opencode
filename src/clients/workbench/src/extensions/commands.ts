import { toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { CommandsRegistry, type ICommand, type ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { ImplicitActivationEvents } from '../../upstream/src/vs/platform/extensionManagement/common/implicitActivationEvents.js';
import { IInstantiationService, type ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ExtensionHostKind } from '../../upstream/src/vs/workbench/services/extensions/common/extensionHostKind.js';
import { IExtensionService, type IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { workspaceFileSource } from '../workspaceFiles.js';

class ExtensionCommandSource {
  constructor(readonly host: IExtensionHost, readonly args: unknown[]) { }
}

export function executeHostCommand<T>(commands: ICommandService, host: IExtensionHost, id: string, args: unknown[]): Promise<T | undefined> {
  return commands.executeCommand<T>(id, new ExtensionCommandSource(host, args));
}

export function unpackCommandSource(args: unknown[]): { source?: IExtensionHost; args: unknown[] } {
  const value = args[0];
  return args.length === 1 && value instanceof ExtensionCommandSource ? { source: value.host, args: value.args } : { args };
}

/** 在首次 await 前捕获编辑器 URI，选定后保留原 host 对象，以阻止 reload 期间转投新代际。 */
export async function captureCommandHost(service: IExtensionService, id: string, resource: () => URI | undefined,
  picker: () => IQuickInputService, source?: IExtensionHost): Promise<IExtensionHost | undefined> {
  if (!source && CommandsRegistry.getCommand(id) && !commandHosts(service, id, service.getExtensionHosts()).length) return undefined;
  const activeResource = source ? undefined : resource();
  await service.whenInstalledExtensionsRegistered();
  const hosts = service.getExtensionHosts();
  if (source && !hosts.includes(source)) throw new Error('发起命令的扩展宿主代际已经关闭');
  const candidates = commandHosts(service, id, hosts);
  if (source) {
    if (candidates.includes(source)) return source;
    if (!candidates.length) return id === '_executeWorkspaceSymbolProvider' ? source : undefined;
    const legal = candidates.filter(host => host.extensions?.allExtensions.some(extension => host.extensions!.containsExtension(extension.identifier)
      && source.extensions?.allExtensions.some(visible => visible.identifier.value.toLowerCase() === extension.identifier.value.toLowerCase()
        && JSON.stringify(visible.installationTarget) === JSON.stringify(extension.installationTarget))));
    return chooseUnique(legal, id);
  }
  if (activeResource?.scheme === 'boxteam-file') {
    const owner = workspaceFileSource(activeResource);
    const matching = candidates.filter(host => {
      const location = host.runningLocation;
      if (location.kind !== ExtensionHostKind.Remote && location.kind !== ExtensionHostKind.LocalWebWorker) return false;
      return location.installationTarget?.gatewayId === owner.gatewayId && !!location.workspaceIds?.includes(owner.workspaceId);
    });
    if (matching.length) return chooseUnique(matching, id);
  }
  if (candidates.length < 2) return candidates[0];
  const picked = await picker().pick(candidates.map(host => {
    const location = host.runningLocation;
    const target = location.kind === ExtensionHostKind.Remote || location.kind === ExtensionHostKind.LocalWebWorker ? location.installationTarget : undefined;
    return { host, label: target ? target.gatewayId + ' / ' + target.backendId : 'Web',
      description: target && location.kind !== ExtensionHostKind.LocalProcess ? location.workspaceIds!.join(', ') : undefined };
  }), { placeHolder: '为命令 ' + id + ' 选择目标；本次调用将固定该宿主' });
  if (!picked) throw new Error('命令目标选择已取消');
  if (!service.getExtensionHosts().includes(picked.host)) throw new Error('选定的扩展宿主代际已经关闭');
  return picked.host;
}

function commandHosts(service: IExtensionService, id: string, hosts: readonly IExtensionHost[]): IExtensionHost[] {
  // 首次命令可以先于宿主绑定；候选直接读原 registry 的运行位置投影。
  return hosts.filter(host => commands.get(service)?.get(id)?.sources.has(host) || service.getHostExtensions(host).some(extension => {
    return ImplicitActivationEvents.readActivationEvents(extension).includes('onCommand:' + id)
      || extension.contributes?.commands && (Array.isArray(extension.contributes.commands) ? extension.contributes.commands : [extension.contributes.commands]).some(command => command.command === id);
  }));
}

function chooseUnique(hosts: IExtensionHost[], id: string): IExtensionHost {
  if (hosts.length !== 1) throw new Error('命令在发起视图内仍有多个合法宿主：' + id);
  return hosts[0];
}

type Handler = (...args: unknown[]) => Promise<unknown>;
interface SharedCommand { registration: IDisposable; sources: Map<IExtensionHost, Handler> }
const commands = new WeakMap<IExtensionService, Map<string, SharedCommand>>();
const dispatchers = new WeakMap<Function, (accessor: ServicesAccessor, args: unknown[], host?: IExtensionHost) => Promise<unknown>>();

/** 同一窗口命令 ID 只有一个原注册入口；dispose 只撤销自己的来源和代际。 */
export function registerTargetCommand(service: IExtensionService, host: IExtensionHost, id: string, handler: Handler): IDisposable {
  let registry = commands.get(service);
  if (!registry) { registry = new Map(); commands.set(service, registry); }
  let entry = registry.get(id);
  if (!entry) {
    const sources = new Map<IExtensionHost, Handler>();
    const dispatch = async (accessor: ServicesAccessor, args: unknown[], frozen?: IExtensionHost) => {
      const editor = EditorResourceAccessor.getCanonicalUri(accessor.get(IEditorService).activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
      const instantiation = accessor.get(IInstantiationService);
      const target = frozen ?? await captureCommandHost(service, id, () => editor,
        () => instantiation.invokeFunction(accessor => accessor.get(IQuickInputService)));
      if (!target || !service.getExtensionHosts().includes(target)) throw new Error('命令目标宿主已经关闭或缺少明确归属：' + id);
      const execute = sources.get(target);
      if (!execute) throw new Error('选定宿主尚未注册命令：' + id);
      return execute(...args);
    };
    const sharedHandler = (accessor: ServicesAccessor, ...args: unknown[]) => dispatch(accessor, args);
    dispatchers.set(sharedHandler, dispatch);
    entry = { sources, registration: CommandsRegistry.registerCommand(id, sharedHandler) };
    registry.set(id, entry);
  }
  if (entry.sources.has(host)) throw new Error('同一宿主重复注册命令：' + id);
  entry.sources.set(host, handler);
  const owned = entry;
  return toDisposable(() => {
    if (owned.sources.get(host) !== handler) return;
    owned.sources.delete(host);
    if (!owned.sources.size) { owned.registration.dispose(); registry!.delete(id); }
  });
}

export function executeCommandForHost(command: ICommand, accessor: ServicesAccessor, args: unknown[], host?: IExtensionHost): unknown {
  const dispatcher = dispatchers.get(command.handler);
  // 原内置符号命令没有资源参数；私有来源只在同窗口传给原 handler，不进入扩展 RPC。
  if (!dispatcher && host && command.id === '_executeWorkspaceSymbolProvider') {
    if (!accessor.get(IExtensionService).getExtensionHosts().includes(host)) throw new Error('发起符号查询的扩展宿主代际已经关闭');
    return command.handler(accessor, args[0], new ExtensionCommandSource(host, []));
  }
  return dispatcher ? dispatcher(accessor, args, host) : command.handler(accessor, ...args);
}
