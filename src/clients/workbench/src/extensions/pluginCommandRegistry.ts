import { DisposableStore, toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { KeybindingParser } from '../../upstream/src/vs/base/common/keybindingParser.js';
import { KeyCode, ScanCode } from '../../upstream/src/vs/base/common/keyCodes.js';
import { KeyCodeChord, ScanCodeChord } from '../../upstream/src/vs/base/common/keybindings.js';
import { isMacintosh, isWindows } from '../../upstream/src/vs/base/common/platform.js';
import { MenuId, MenuRegistry, MenuItemAction, type IMenuService } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ICommandAction } from '../../upstream/src/vs/platform/action/common/action.js';
import { CommandsRegistry } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { ContextKeyExpr, Parser, type ContextKeyExpression, type IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { KeybindingsRegistry } from '../../upstream/src/vs/platform/keybinding/common/keybindingsRegistry.js';
import type { PluginCommandPublication, PluginCommandRegistration, PluginContributionTarget, PluginCommandSnapshotDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/plugin_commands.js';
import type { PluginCommandContribution } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';

export const pluginGatewayContextKey = 'boxteam.pluginGateway';
export const pluginWorkspaceContextKey = 'boxteam.pluginWorkspace';
/** 首批公布的上下文和菜单；不因为上游支持更多位置而隐式开放。 */
export const pluginCommandContextKeys = new Set([
  pluginGatewayContextKey, pluginWorkspaceContextKey, 'editorTextFocus', 'editorHasSelection',
  'resourceScheme', 'resourceExtname', 'resourceFilename', 'isLinux', 'isWindows', 'isMac',
]);
export const pluginCommandMenus = new Map([
  ['commandPalette', MenuId.CommandPalette], ['editor/title', MenuId.EditorTitle], ['editor/context', MenuId.EditorContext],
]);

export interface PluginCommandOwner { gatewayId: string; workspaceId?: string }

/** Quick Access 在聚焦前捕获的 owner 沿同次模式转接保留，不读管理页选择。 */
export function pluginCommandPaletteContext(context: IContextKeyService, options?: object): IContextKeyService {
  const invocation = options as { pluginCommandOwner?: PluginCommandOwner; pluginCommandContext?: readonly [string, unknown][] } | undefined;
  const owner = invocation?.pluginCommandOwner;
  const captured = new Map(invocation?.pluginCommandContext);
  return context.createOverlay([...pluginCommandContextKeys].map(key => [key,
    key === pluginGatewayContextKey ? owner?.gatewayId ?? ''
      : key === pluginWorkspaceContextKey ? owner?.workspaceId ?? ''
        : captured.has(key) ? captured.get(key) : context.getContextKeyValue(key)] as [string, unknown]));
}

/** 原生命令保持原 scoped context；只有自有贡献使用打开浮层前的冻结条件。 */
export function pluginCommandPaletteActions(menus: IMenuService, live: IContextKeyService, frozen: IContextKeyService) {
  const native = menus.getMenuActions(MenuId.CommandPalette, live).map(([group, actions]) =>
    [group, actions.filter(action => !(action instanceof MenuItemAction && action.item.pluginCommandArgs))] as const);
  const plugins = menus.getMenuActions(MenuId.CommandPalette, frozen).map(([group, actions]) =>
    [group, actions.filter(action => action instanceof MenuItemAction && !!action.item.pluginCommandArgs)] as const);
  return [...native, ...plugins];
}

export function isPluginCommandId(id: string): boolean { return id.startsWith('boxteam.plugin:'); }

export function pluginCommandId(packageId: string, pluginId: string, commandId: string): string {
  return 'boxteam.plugin:' + [packageId, pluginId, commandId].map(encodeURIComponent).join('/');
}

export function pluginTargetKey(target: PluginContributionTarget): string {
  return JSON.stringify([target.gateway_id, target.workspace_id ?? null]);
}

function condition(value?: string): ContextKeyExpression | undefined {
  if (value == null) return undefined;
  const parser = new Parser({ regexParsingWithErrorRecovery: false });
  const expression = parser.parse(value);
  if (!expression || parser.lexingErrors.length || parser.parsingErrors.length) throw new Error('插件条件表达式无效：' + value);
  const unknown = expression.keys().filter(key => !pluginCommandContextKeys.has(key));
  if (unknown.length) throw new Error('插件条件使用未公布的上下文键：' + unknown.join(', '));
  return expression;
}

function keybinding(value?: string) {
  if (value == null) return undefined;
  const binding = KeybindingParser.parseKeybinding(value);
  if (!binding || binding.chords.some(chord => chord instanceof KeyCodeChord && chord.keyCode === KeyCode.Unknown
    || chord instanceof ScanCodeChord && chord.scanCode === ScanCode.None)) throw new Error('插件快捷键无效：' + value);
  return binding;
}

export interface FrozenPluginCommand {
  readonly target: PluginContributionTarget;
  readonly publication: PluginCommandPublication;
  readonly command: PluginCommandContribution;
  readonly registration?: PluginCommandRegistration;
}
class PluginCommandSource implements FrozenPluginCommand {
  constructor(readonly target: PluginContributionTarget, readonly publication: PluginCommandPublication,
    readonly command: PluginCommandContribution, readonly registration?: PluginCommandRegistration) { Object.freeze(this); }
}
interface Publication {
  signature: string;
  handles: DisposableStore;
}

/** 仅持有原 Registry 的窗口句柄；执行事实始终来自 Gateway 的完整快照。 */
export class PluginCommandContributions implements IDisposable {
  private readonly targets = new Map<string, Map<string, Publication>>();
  private readonly dispatchers = new Map<string, { handle: IDisposable; sources: Set<PluginCommandSource> }>();

  constructor(private readonly owner: (accessor: ServicesAccessor) => PluginCommandOwner,
    private readonly execute: (accessor: ServicesAccessor, source: FrozenPluginCommand, args: unknown[]) => Promise<unknown>) { }

  replace(target: PluginContributionTarget, snapshot: PluginCommandSnapshotDTO): void {
    if (snapshot.gateway_id !== target.gateway_id || (snapshot.workspace_id ?? undefined) !== target.workspace_id) throw new Error('插件快照目标不符');
    const targetKey = pluginTargetKey(target), previous = this.targets.get(targetKey) ?? new Map<string, Publication>();
    // 工作区快照也带 Gateway 声明；该部分只由唯一 Gateway 订阅发布，避免重复句柄。
    const selected = snapshot.publications.filter(item => (item.binding?.workspace_id ?? undefined) === target.workspace_id);
    const candidates = selected.map(item => this.prepare(target, item));
    if (new Set(candidates.map(item => item.key)).size !== candidates.length) throw new Error('插件快照重复绑定');
    this.targets.set(targetKey, previous);
    for (const [key, entry] of previous) if (!candidates.some(item => item.key === key)) {
      entry.handles.dispose(); previous.delete(key);
    }
    for (const candidate of candidates) {
      if (previous.get(candidate.key)?.signature === candidate.signature) continue;
      previous.get(candidate.key)?.handles.dispose();
      const entry: Publication = { signature: candidate.signature, handles: new DisposableStore() };
      previous.set(candidate.key, entry);
      try {
        // 固定先命令，再发布引用；失败只释放本次新增，不复原已释放的旧声明。
        for (const command of candidate.commands) {
          entry.handles.add(this.register(command.source));
        }
        for (const command of candidate.commands) {
          const menus = candidate.menus.filter(menu => menu.command_id === command.source.command.id);
          if (!menus.some(menu => menu.location === 'commandPalette')) {
            entry.handles.add(MenuRegistry.appendMenuItem(MenuId.CommandPalette, { command: command.action, when: candidate.scope }));
          }
          for (const menu of menus) entry.handles.add(MenuRegistry.appendMenuItem(menu.menuId, {
            command: command.action, when: ContextKeyExpr.and(candidate.scope, menu.condition), group: menu.group, order: menu.order,
          }));
          for (const rule of candidate.keybindings.filter(rule => rule.command_id === command.source.command.id)) {
            if (rule.parsed) entry.handles.add(KeybindingsRegistry.registerPluginKeybinding(rule.parsed, command.action.id, command.source,
              ContextKeyExpr.and(command.action.precondition, rule.condition)));
          }
        }
      } catch (error) {
        entry.handles.dispose();
        previous.delete(candidate.key);
        throw error;
      }
    }
  }

  private prepare(target: PluginContributionTarget, publication: PluginCommandPublication) {
    const binding = publication.binding;
    if (!binding?.enabled || (binding.workspace_id ?? undefined) !== target.workspace_id) throw new Error('插件声明缺少启用绑定或目标不符');
    const key = JSON.stringify([binding.package_id, binding.plugin_id]);
    const scope = ContextKeyExpr.and(ContextKeyExpr.equals(pluginGatewayContextKey, target.gateway_id),
      target.workspace_id ? ContextKeyExpr.equals(pluginWorkspaceContextKey, target.workspace_id) : undefined)!;
    if (new Set(publication.commands.map(command => command.id)).size !== publication.commands.length) throw new Error('插件命令声明重复');
    const ids = new Set(publication.commands.map(command => command.id));
    for (const entries of [publication.menus, publication.keybindings]) {
      if (new Set(entries.map(item => item.id)).size !== entries.length) throw new Error('插件贡献声明重复');
    }
    const registrations = new Map(publication.registrations.map(registration => [registration.command_id, registration]));
    if (registrations.size !== publication.registrations.length || publication.registrations.some(reg => !ids.has(reg.command_id))) throw new Error('插件命令注册不属于声明');
    const menus = publication.menus.map(menu => {
      const menuId = pluginCommandMenus.get(menu.location);
      if (!menuId || !ids.has(menu.command_id)) throw new Error('插件菜单位置或引用未开放：' + menu.location);
      return { ...menu, menuId, condition: condition(menu.when) };
    });
    const keybindings = publication.keybindings.map(rule => {
      if (!ids.has(rule.command_id)) throw new Error('插件快捷键引用未声明命令');
      const parsed = new Map([rule.key, rule.mac, rule.win, rule.linux].map(value => [value, keybinding(value)]));
      return { ...rule, condition: condition(rule.when),
        parsed: parsed.get(isMacintosh ? rule.mac ?? rule.key : isWindows ? rule.win ?? rule.key : rule.linux ?? rule.key) };
    });
    const commands = publication.commands.map(command => {
      const id = pluginCommandId(binding.package_id, binding.plugin_id, command.id);
      if (CommandsRegistry.getCommand(id) && !this.dispatchers.has(id)) throw new Error('插件命令不能覆盖已有入口：' + id);
      const registration = registrations.get(command.id);
      if (registration && registration.handler !== command.handler) throw new Error('插件处理者与声明不符');
      const source = new PluginCommandSource(Object.freeze({ ...target }), publication, command, registration);
      const available = publication.state === 'ready' && !!publication.instance_id && !!registration;
      const action: ICommandAction = { id, title: command.title, category: command.category,
        precondition: ContextKeyExpr.and(scope, condition(command.enablement), available ? undefined : ContextKeyExpr.false()),
        pluginCommandArgs: [source] };
      return { source, action };
    });
    return { key, signature: JSON.stringify(publication), commands, scope, menus, keybindings };
  }

  private register(source: PluginCommandSource): IDisposable {
    const binding = source.publication.binding!, id = pluginCommandId(binding.package_id, binding.plugin_id, source.command.id);
    let dispatcher = this.dispatchers.get(id);
    if (!dispatcher) {
      const sources = new Set<PluginCommandSource>();
      const handle = CommandsRegistry.registerCommand(id, (accessor, ...args: unknown[]) => {
        const frozen = args[0] instanceof PluginCommandSource ? args.shift() as PluginCommandSource : undefined;
        const owner = frozen ? undefined : this.owner(accessor);
        const candidates = frozen ? [frozen] : [...sources].filter(item => item.target.gateway_id === owner!.gatewayId
          && (!item.target.workspace_id || item.target.workspace_id === owner!.workspaceId));
        if (candidates.length !== 1) throw new Error('命令缺少唯一明确的插件目标：' + id);
        this.assertCurrent(candidates[0]);
        return this.execute(accessor, candidates[0], args);
      });
      dispatcher = { sources, handle }; this.dispatchers.set(id, dispatcher);
    }
    const owned = dispatcher;
    owned.sources.add(source);
    return toDisposable(() => {
      owned.sources.delete(source);
      if (!owned.sources.size) { owned.handle.dispose(); this.dispatchers.delete(id); }
    });
  }

  assertCurrent(source: FrozenPluginCommand): void {
    const binding = source.publication.binding!, id = pluginCommandId(binding.package_id, binding.plugin_id, source.command.id);
    if (!this.dispatchers.get(id)?.sources.has(source as PluginCommandSource)) throw new Error('原插件命令绑定或注册已经失效，请重新选择');
    if (source.publication.state !== 'ready' || !source.publication.instance_id || !source.registration) {
      throw new Error('插件命令尚不可执行：' + (source.publication.error ?? source.publication.state));
    }
  }

  remove(target: PluginContributionTarget): void {
    const key = pluginTargetKey(target), publications = this.targets.get(key);
    if (publications) for (const entry of publications.values()) entry.handles.dispose();
    this.targets.delete(key);
  }

  dispose(): void { for (const publications of this.targets.values()) for (const entry of publications.values()) entry.handles.dispose(); this.targets.clear(); }
}
