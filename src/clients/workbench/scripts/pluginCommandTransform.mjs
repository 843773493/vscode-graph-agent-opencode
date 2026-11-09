import { join } from 'node:path';

export const pluginCommandSourcePaths = [
  'src/vs/platform/action/common/action.ts',
  'src/vs/platform/actions/common/actions.ts',
  'src/vs/platform/keybinding/common/keybindingsRegistry.ts',
  'src/vs/workbench/services/keybinding/browser/keybindingService.ts',
  'src/vs/platform/quickinput/browser/commandsQuickAccess.ts',
  'src/vs/workbench/contrib/quickaccess/browser/commandsQuickAccess.ts',
  'src/vs/workbench/services/commands/common/commandService.ts',
];

/** 自有贡献直接接原服务；只增加冻结命令参数和可释放的原键位登记。 */
export function transformPluginCommands(root, path, source, replaceOnce) {
  const file = relative => path === join(root, 'upstream', relative);
  let contents = source;
  if (file(pluginCommandSourcePaths[0])) {
    contents = replaceOnce(contents, 'export interface ICommandAction {',
      'export interface ICommandAction {\n/** 自有插件的窗口内冻结调用资格，不持久化。 */\nreadonly pluginCommandArgs?: readonly unknown[];');
  }
  if (file(pluginCommandSourcePaths[1])) {
    contents = replaceOnce(contents, 'let runArgs: unknown[] = [];', 'if (this.item.pluginCommandArgs) return this._commandService.executeCommand(this.id, ...this.item.pluginCommandArgs);\nlet runArgs: unknown[] = [];');
  }
  if (file(pluginCommandSourcePaths[2])) {
    contents = replaceOnce(contents, "import { Registry } from '../../registry/common/platform.js';",
      "import { Registry } from '../../registry/common/platform.js';\nimport { Emitter } from '../../../base/common/event.js';");
    contents = replaceOnce(contents, 'export interface IKeybindingsRegistry {',
      `export interface IKeybindingsRegistry {
readonly onDidChangePluginKeybindings: import('../../../base/common/event.js').Event<void>;
registerPluginKeybinding(keybinding: Keybinding, commandId: string, args: unknown, when: ContextKeyExpression | undefined): IDisposable;`);
    contents = replaceOnce(contents, 'class KeybindingsRegistryImpl implements IKeybindingsRegistry {',
      `class KeybindingsRegistryImpl implements IKeybindingsRegistry {
private readonly pluginKeybindingsChanged = new Emitter<void>();
readonly onDidChangePluginKeybindings = this.pluginKeybindingsChanged.event;
registerPluginKeybinding(keybinding: Keybinding, commandId: string, args: unknown, when: ContextKeyExpression | undefined): IDisposable {
const handle = this._registerDefaultKeybinding(keybinding, commandId, args, KeybindingWeight.ExternalExtension, 0, when);
this.pluginKeybindingsChanged.fire();
return toDisposable(() => { handle.dispose(); this.pluginKeybindingsChanged.fire(); });
}`);
  }
  if (file(pluginCommandSourcePaths[3])) {
    contents = replaceOnce(contents, 'this._keybindingHoldMode = null;\n\t\tthis._cachedResolver = null;',
      'this._keybindingHoldMode = null;\n\t\tthis._cachedResolver = null;\nthis._register(KeybindingsRegistry.onDidChangePluginKeybindings(() => this.updateResolver()));');
  }
  if (file(pluginCommandSourcePaths[4])) {
    contents = replaceOnce(contents, 'await this.getCommandPicks(token)', 'await this.getCommandPicks(token, runOptions)');
    contents = replaceOnce(contents, 'getCommandPicks(token: CancellationToken): Promise<Array<ICommandQuickPick>>;',
      'getCommandPicks(token: CancellationToken, runOptions?: IQuickAccessProviderRunOptions): Promise<Array<ICommandQuickPick>>;');
  }
  if (file(pluginCommandSourcePaths[5])) {
    contents = `import { pluginCommandPaletteContext, pluginCommandPaletteActions } from ${JSON.stringify(join(root, 'src/extensions/pluginCommandRegistry.js'))};\n` + contents;
    contents = replaceOnce(contents, "import { Action2, IMenuService, MenuId, MenuItemAction, SubmenuItemAction } from '../../../../platform/actions/common/actions.js';",
      "import { Action2, IMenuService, MenuItemAction, SubmenuItemAction } from '../../../../platform/actions/common/actions.js';");
    contents = replaceOnce(contents, 'protected async getCommandPicks(token: CancellationToken): Promise<Array<ICommandQuickPick>> {',
      `protected async getCommandPicks(token: CancellationToken, runOptions?: import('../../../../platform/quickinput/common/quickAccess.js').IQuickAccessProviderRunOptions): Promise<Array<ICommandQuickPick>> {
const commandContext = this.editorService.activeEditorPane?.scopedContextKeyService || this.editorGroupService.activeGroup.scopedContextKeyService;
const pluginContext = pluginCommandPaletteContext(commandContext, runOptions);`);
    contents = replaceOnce(contents, '...this.getGlobalCommandPicks()', '...this.getGlobalCommandPicks(commandContext, pluginContext)');
    contents = replaceOnce(contents, 'private getGlobalCommandPicks(): ICommandQuickPick[] {',
      `private getGlobalCommandPicks(scopedContextKeyService: import('../../../../platform/contextkey/common/contextkey.js').IContextKeyService, pluginContext: import('../../../../platform/contextkey/common/contextkey.js').IContextKeyService): ICommandQuickPick[] {`);
    contents = replaceOnce(contents, '\t\tconst scopedContextKeyService = this.editorService.activeEditorPane?.scopedContextKeyService || this.editorGroupService.activeGroup.scopedContextKeyService;\n', '');
    contents = replaceOnce(contents, 'this.menuService.getMenuActions(MenuId.CommandPalette, scopedContextKeyService)', 'pluginCommandPaletteActions(this.menuService, scopedContextKeyService, pluginContext)');
    contents = replaceOnce(contents, 'commandId: action.item.id,', 'commandId: action.item.id,\nargs: action.item.pluginCommandArgs ? [...action.item.pluginCommandArgs] : undefined,');
  }
  if (file(pluginCommandSourcePaths[6])) {
    contents = `import { isPluginCommandId } from ${JSON.stringify(join(root, 'src/extensions/pluginCommandRegistry.js'))};\n` + contents;
    contents = replaceOnce(contents, "this._logService.trace('CommandService#executeCommand', id);",
      "this._logService.trace('CommandService#executeCommand', id);\nif (isPluginCommandId(id)) return this._tryExecuteCommand(id, args);");
  }
  return contents;
}
