import { join } from 'node:path';

export const harnessWelcomeSourcePaths = [
  'src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts',
];

/** 在 Getting Started 原入口直达产品内置的 Harness 管理命令。 */
export function transformHarnessWelcome(root, path, contents, replaceOnce) {
  if (path !== join(root, 'upstream', harnessWelcomeSourcePaths[0])) return contents;
  return replaceOnce(contents,
    'export const startEntries: GettingStartedStartEntryContent = [\n',
    `export const startEntries: GettingStartedStartEntryContent = [
	{
		id: 'boxteam.manageProviders',
		title: localize('boxteam.manageProviders.title', "Manage Model Providers"),
		description: localize('boxteam.manageProviders.description', "Configure model providers for BoxTeam."),
		icon: Codicon.settingsGear,
		content: { type: 'startEntry', command: 'command:boxteam.settings.modelsProviders' }
	},
	{
		id: 'boxteam.manageAgents',
		title: localize('boxteam.manageAgents.title', "Manage Agents"),
		description: localize('boxteam.manageAgents.description', "Configure agents for BoxTeam."),
		icon: Codicon.settingsGear,
		content: { type: 'startEntry', command: 'command:boxteam.settings.agents' }
	},
	{
		id: 'boxteam.manageGateway',
		title: localize('boxteam.manageGateway.title', "Manage Gateway"),
		description: localize('boxteam.manageGateway.description', "Manage Gateway connections and credentials."),
		icon: Codicon.settingsGear,
		content: { type: 'startEntry', command: 'command:boxteam.gateway.connections' }
	},
`);
}
