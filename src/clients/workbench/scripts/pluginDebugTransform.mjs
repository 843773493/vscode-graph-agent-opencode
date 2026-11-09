import { join } from 'node:path';

export const pluginDebugSourcePaths = [
  'src/vs/workbench/contrib/debug/common/debug.ts',
  'src/vs/workbench/contrib/debug/common/debugger.ts',
  'src/vs/workbench/contrib/debug/common/debugTelemetry.ts',
  'src/vs/workbench/contrib/debug/common/nullDebugService.ts',
  'src/vs/workbench/contrib/debug/browser/debugAdapterManager.ts',
  'src/vs/workbench/contrib/debug/browser/debugService.ts',
  'src/vs/workbench/contrib/debug/browser/debugProgress.ts',
  'src/vs/workbench/contrib/debug/browser/breakpointEditorContribution.ts',
  'src/vs/workbench/contrib/debug/browser/breakpointsView.ts',
  'src/vs/workbench/contrib/debug/browser/debugSession.ts',
  'src/vs/workbench/contrib/debug/browser/debugConfigurationManager.ts',
];

/** 自有调试声明直接进入原 registry；不伪造 VSIX 描述，不合并不同目标的 schema。 */
export function transformPluginDebug(root, path, source, replaceOnce) {
  const index = pluginDebugSourcePaths.findIndex(relative => path === join(root, 'upstream', relative));
  if (index < 0) return source;
  let contents = source;
  if (index === 0) {
    contents = replaceOnce(contents, '// internals\n\t__configurationTarget?',
      '// internals\n\tpluginDebuggerRegistration?: string;\n\t__configurationTarget?');
    contents = replaceOnce(contents, 'getDebuggerLabel(type: string)', 'getDebuggerLabel(type: string, config?: IConfig)');
    contents = replaceOnce(contents, 'getDebugger(type: string)', 'getDebugger(type: string, config?: IConfig)');
    contents = replaceOnce(contents, 'getEnabledDebugger(type: string)', 'getEnabledDebugger(type: string, config?: IConfig)');
    contents = replaceOnce(contents, 'registerDebugAdapterFactory(debugTypes:',
      'registerPluginDebugger(registrationId: string, contribution: IDebuggerContribution, factory: IDebugAdapterFactory): IDisposable;\n\tregisterDebugAdapterFactory(debugTypes:');
  } else if (index === 1) {
    contents = replaceOnce(contents, 'extensionDescription: IExtensionDescription,', 'extensionDescription: IExtensionDescription | undefined,');
    contents = replaceOnce(contents, 'this.merge(dbgContribution, extensionDescription);',
      'if (extensionDescription) this.merge(dbgContribution, extensionDescription);\n\t\telse this.debuggerContribution = dbgContribution;');
    contents = replaceOnce(contents, "await this.adapterManager.activateDebuggers('onDebugAdapterProtocolTracker', this.type);",
      "if (!session.configuration.pluginDebuggerRegistration) await this.adapterManager.activateDebuggers('onDebugAdapterProtocolTracker', this.type);");
    contents = replaceOnce(contents, 'return await this.configurationResolverService.resolveWithInteractionReplace(',
      'if (config.pluginDebuggerRegistration) return substitutedConfig;\n\t\treturn await this.configurationResolverService.resolveWithInteractionReplace(');
    contents = replaceOnce(contents, 'getMainExtensionDescriptor(): IExtensionDescription {', 'getMainExtensionDescriptor(): IExtensionDescription | undefined {');
    contents = replaceOnce(contents, 'if (!aiKey) {', 'if (!aiKey || !this.getMainExtensionDescriptor()) {');
    contents = replaceOnce(contents, 'this.getMainExtensionDescriptor().publisher', 'this.getMainExtensionDescriptor()!.publisher');
  } else if (index === 2) {
    contents = replaceOnce(contents, 'const extension = dbgr.getMainExtensionDescriptor();',
      'const extension = dbgr.getMainExtensionDescriptor();\n\t\tif (!extension) return;');
  } else if (index === 3) {
    contents = replaceOnce(contents, 'registerDebugAdapterFactory() {', 'registerPluginDebugger() { return Disposable.None; },\n\tregisterDebugAdapterFactory() {');
  } else if (index === 4) {
    contents = replaceOnce(contents, 'IDebugSession, IGuessedDebugger,', 'IDebugSession, IDebuggerContribution, IGuessedDebugger,');
    contents = replaceOnce(contents, 'private debuggers: Debugger[];',
      'private debuggers: Debugger[];\n\tprivate readonly pluginDebuggers = new Map<string, { debugger: Debugger; references: number }>();');
    contents = replaceOnce(contents, 'Disposable, IDisposable }', 'Disposable, IDisposable, toDisposable }');
    contents = replaceOnce(contents, '\n\tregisterDebugAdapterFactory(', `
  registerPluginDebugger(registrationId: string, contribution: IDebuggerContribution, factory: IDebugAdapterFactory): IDisposable {
    const currentFactory = this.debugAdapterFactories.get(contribution.type);
    if (currentFactory && currentFactory !== factory) throw new Error('调试类型已有不同 dispatcher：' + contribution.type);
    let entry = this.pluginDebuggers.get(registrationId);
    if (!entry) {
      entry = { debugger: this.instantiationService.createInstance(Debugger, this, contribution, undefined), references: 0 };
      this.pluginDebuggers.set(registrationId, entry);
      this.debugAdapterFactories.set(contribution.type, factory);
      this.debuggersAvailable.set(this.hasEnabledDebuggers());
      this._onDidRegisterDebugger.fire();
    }
    entry.references++;
    return toDisposable(() => {
      if (--entry.references > 0) return;
      this.pluginDebuggers.delete(registrationId);
      if (![...this.pluginDebuggers.values()].some(value => value.debugger.type === contribution.type)) {
        this.debugAdapterFactories.delete(contribution.type);
      }
      this.debuggersAvailable.set(this.hasEnabledDebuggers());
      this._onDidRegisterDebugger.fire();
    });
  }

\tregisterDebugAdapterFactory(`);
    contents = replaceOnce(contents, 'hasEnabledDebuggers(): boolean {',
      'hasEnabledDebuggers(): boolean {\n    if ([...this.pluginDebuggers.values()].some(d => d.debugger.enabled && this.debugAdapterFactories.has(d.debugger.type))) return true;');
    contents = replaceOnce(contents, 'getDebuggerLabel(type: string): string | undefined {\n\t\tconst dbgr = this.getDebugger(type);',
      'getDebuggerLabel(type: string, config?: IConfig): string | undefined {\n\t\tconst dbgr = this.getDebugger(type, config);');
    contents = replaceOnce(contents, 'return this.breakpointContributions.some(',
      "return (model.uri.scheme === 'boxteam-file' && ['javascript', 'javascriptreact'].includes(languageId)) || this.breakpointContributions.some(");
    contents = replaceOnce(contents, 'getDebugger(type: string): Debugger | undefined {', `getDebugger(type: string, config?: IConfig): Debugger | undefined {
    if (config?.pluginDebuggerRegistration) {
      const value = this.pluginDebuggers.get(config.pluginDebuggerRegistration)?.debugger;
      return value?.type === type ? value : undefined;
    }`);
    contents = replaceOnce(contents, 'getEnabledDebugger(type: string): Debugger | undefined {\n\t\tconst adapter = this.getDebugger(type);',
      'getEnabledDebugger(type: string, config?: IConfig): Debugger | undefined {\n\t\tconst adapter = this.getDebugger(type, config);');
    contents = replaceOnce(contents, 'if (descriptor.id &&', 'if (descriptor?.id &&');
    // 释放旧 factory 不能删除后来合法注册的同类型 factory。
    contents = replaceOnce(contents, 'debugTypes.forEach(debugType => this.debugAdapterFactories.delete(debugType));',
      'debugTypes.forEach(debugType => { if (this.debugAdapterFactories.get(debugType) === debugAdapterLauncher) this.debugAdapterFactories.delete(debugType); });\n          this.debuggersAvailable.set(this.hasEnabledDebuggers());');
  } else if (index === 5) {
    contents = replaceOnce(contents, "await this.extensionService.activateByEvent('onDebug');",
      "if (typeof configOrName === 'string' || !configOrName?.pluginDebuggerRegistration) await this.extensionService.activateByEvent('onDebug');");
    contents = replaceOnce(contents, 'await this.extensionService.whenInstalledExtensionsRegistered();',
      "if (typeof configOrName === 'string' || !configOrName?.pluginDebuggerRegistration) await this.extensionService.whenInstalledExtensionsRegistered();");
    for (const config of ['resolvedConfig', 'session.configuration', 'config']) {
      contents = replaceOnce(contents, `this.adapterManager.getDebugger(${config}.type)`, `this.adapterManager.getDebugger(${config}.type, ${config})`);
    }
  } else if (index === 6) {
    contents = replaceOnce(contents, 'getDebuggerLabel(session.configuration.type)', 'getDebuggerLabel(session.configuration.type, session.configuration)');
  } else if (index === 7) {
    contents = replaceOnce(contents, 'getDebugger(s.configuration.type)', 'getDebugger(s.configuration.type, s.configuration)');
  } else if (index === 8) {
    contents = replaceOnce(contents, 'getDebugger(currentType)', 'getDebugger(currentType, this.debugService.getViewModel().focusedSession?.configuration)');
  } else if (index === 9) {
    contents = `import { acceptsPluginDebugSource } from ${JSON.stringify(join(root, 'src/debug/adapter.js'))};\n` + contents;
    contents = replaceOnce(contents, 'async sendBreakpoints(modelUri: URI, breakpointsToSend: IBreakpoint[], sourceModified: boolean): Promise<void> {',
      'async sendBreakpoints(modelUri: URI, breakpointsToSend: IBreakpoint[], sourceModified: boolean): Promise<void> {\n    if (!acceptsPluginDebugSource(this.configuration, modelUri)) return;');
  } else if (index === 10) {
    // 自有配置由目标 Backend 解释；旧窗口／VSIX 的通配 Provider 不参与这条链。
    for (const method of ['resolveConfigurationByProviders', 'resolveDebugConfigurationWithSubstitutedVariables']) {
      const marker = `async ${method}(folderUri: uri | undefined, type: string | undefined, config: IConfig, token: CancellationToken): Promise<IConfig | null | undefined> {`;
      contents = replaceOnce(contents, marker, marker + '\n    if (config.pluginDebuggerRegistration) return config;');
    }
  }
  return contents;
}
