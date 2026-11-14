import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { parseTree, printParseErrorCode } from 'jsonc-parser';

export const actionTitleKeys = [
  'workbench.changes.openPatch', 'workbench.debug.startScript',
  'workbench.identityRecovery.import', 'workbench.mcp.openServer',
  'workbench.workspaceAction.activate', 'workbench.workspaceAction.reconnect', 'workbench.workspaceAction.start',
  'workbench.workspaceAction.details',
  'gateway.control.connections', 'gateway.control.connectionsAndCredentials',
  'workbench.sessionAction.rename', 'workbench.sessionAction.fork', 'workbench.sessionAction.delete',
  'workbench.settings.openPluginParameters', 'workbench.settings.openPluginParametersCategory',
  'workbench.settings.openDefaultAgentSource', 'workbench.settings.openSource',
  'workbench.settings.inspectSourceStatus', 'workbench.settings.rebaseSourceDraft', 'workbench.settings.openTarget',
  'workbench.settings.modelsProviders', 'workbench.settings.agents',
  'quickAccess.mode.sessions', 'quickAccess.mode.threads', 'quickAccess.mode.content',
  'quickAccess.workspace.actionTitle', 'quickAccess.sources.actionTitle', 'quickAccess.automation.actionTitle',
  'plugins.attachWorkspaceTitle', 'plugins.manageSources',
  'workbench.view.sessions', 'workbench.view.automation', 'workbench.view.openAutomation',
  'workbench.view.workspaceTerminal', 'workbench.view.openWorkspaceTerminal',
  'workbench.view.workspaceOutput', 'workbench.view.openWorkspaceOutput', 'workbench.view.ports',
  'workbench.view.openPorts', 'workbench.view.mcp', 'workbench.view.openMcp',
  'workbench.view.browser', 'workbench.view.openBrowser', 'workbench.view.tools', 'workbench.view.openTools',
  'workbench.resource.copyUri', 'workbench.resource.attachToChat',
  'workbench.resource.selectSourcesForChat', 'workbench.resource.pasteSelectionToChat',
  'workbench.resource.focus', 'workbench.thread.clearHistoryCache', 'workbench.thread.inspectHistoryCache',
  'workbench.skills.openGatewaySources', 'workbench.skills.openWorkspaceSources',
  'workbench.skills.refreshGatewaySources', 'workbench.skills.refreshWorkspaceSources',
  'workbench.managementTarget.selectExtensions', 'workbench.managementTarget.selectMcp', 'workbench.managementTarget.selectSettings',
  'workbench.managementTarget.selectTools',
  'tasks.action.details', 'tasks.action.history', 'tasks.action.edit', 'tasks.action.pause',
  'tasks.action.resume', 'tasks.action.check', 'tasks.action.checkOnly', 'tasks.action.terminate',
  'tasks.action.update',
  'resources.keep', 'resources.attachThread', 'resources.detach',
  'resources.stopResource', 'resources.deleteResource',
];

export const bootstrapMessageKeys = [
  'bootstrap.loading', 'bootstrap.readLocaleFailed', 'bootstrap.unsupportedLocale', 'bootstrap.clearLocale',
  'bootstrap.loadChineseFailed', 'bootstrap.loadEnglishFailed', 'bootstrap.retry', 'bootstrap.workbenchLoadFailed',
];

export function parseUniqueJson(path, source) {
  const errors = [];
  const root = parseTree(source, errors, { allowTrailingComma: false, disallowComments: true });
  if (!root || errors.length) throw new Error(`Invalid JSON ${path}: ${errors.map(error => printParseErrorCode(error.error)).join(', ')}`);
  const visit = node => {
    if (node.type === 'object') {
      const keys = new Set();
      for (const property of node.children ?? []) {
        const [key, value] = property.children ?? [];
        if (!key || key.type !== 'string') throw new Error(`Invalid object key ${path}`);
        if (keys.has(key.value)) throw new Error(`Duplicate JSON key ${key.value}: ${path}`);
        keys.add(key.value);
        if (value) visit(value);
      }
      return;
    }
    for (const child of node.children ?? []) visit(child);
  };
  visit(root);
  return JSON.parse(source);
}

/** 从权威英文 catalog 按 key 生成紧凑的上游命令 original 投影。 */
export async function writeActionTitleProjection(root, keys) {
  const i18n = join(root, '../shared/core/i18n');
  const path = join(i18n, 'messages/en.json');
  const messages = parseUniqueJson(path, await readFile(path, 'utf8'));
  const titles = Object.fromEntries(keys.map(key => {
    if (!(key in messages)) throw new Error(`Missing English action title: ${key}`);
    return [key, messages[key]];
  }));
  await writeIfChanged(join(i18n, 'actionTitles.en.json'), `${JSON.stringify(titles)}\n`);
}

/** 从双语 catalog 生成启动阶段所需的最小消息投影。 */
export async function writeBootstrapMessageProjection(root, keys = bootstrapMessageKeys) {
  const i18n = join(root, '../shared/core/i18n');
  const catalogs = Object.fromEntries(await Promise.all(['en', 'zh-cn'].map(async locale => [
    locale, parseUniqueJson(join(i18n, `messages/${locale}.json`), await readFile(join(i18n, `messages/${locale}.json`), 'utf8')),
  ])));
  const projection = Object.fromEntries(Object.entries(catalogs).map(([locale, catalog]) => [locale,
    Object.fromEntries(keys.map(key => {
      if (!(key in catalog)) throw new Error(`Missing bootstrap message: ${key} (${locale})`);
      return [key, catalog[key]];
    })),
  ]));
  await writeIfChanged(join(i18n, 'bootstrapMessages.json'), `${JSON.stringify(projection)}\n`);
}

async function writeIfChanged(path, contents) {
  try {
    if (await readFile(path, 'utf8') === contents) return;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  await writeFile(path, contents);
}
