import type { GatewaySettingsPage } from '../../../web/src/components/workspace/gateway/GatewaySettingsContent.js';
import type { GatewayDiagnosticLogLocateRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { t } from '../../../shared/core/i18n/index.js';

export type SettingsScope = 'gateway' | 'backend' | 'workspace';
export const settingsCategories = [
  { id: 'client', label: 'settings.category.client', scopes: [], groups: [] },
  { id: 'providers', label: 'workbench.settings.modelsProviders', scopes: ['backend', 'workspace'], groups: ['llm'] },
  { id: 'agents', label: 'configuration.entities.agentsTitle', scopes: ['backend', 'workspace'], groups: ['agents', 'default_agent'] },
  { id: 'tools', label: 'settings.category.tools', scopes: ['backend', 'workspace', 'gateway'], groups: ['tooling', 'mcp', 'resources'] },
  { id: 'plugins', label: 'settings.category.plugins', scopes: ['gateway', 'workspace'], groups: ['plugins'] },
  { id: 'connections', label: 'settings.category.connections', scopes: ['gateway'], groups: ['workspaces'] },
  { id: 'observability', label: 'settings.category.observability', scopes: ['gateway', 'backend', 'workspace'], groups: ['logging', 'telemetry', 'statistics', 'development'] },
  { id: 'runtime', label: 'settings.category.runtime', scopes: ['backend', 'workspace', 'gateway'], groups: [] },
] as const;
export type SettingsCategory = typeof settingsCategories[number]['id'];
export interface SettingsCategoryOptions { settingsCategory?: SettingsCategory; managementPage?: GatewaySettingsPage; diagnosticLocation?: GatewayDiagnosticLogLocateRequest }

export function settingsCategory(id: string | undefined) {
  return settingsCategories.find(category => `boxteam.settings.category.${category.id}` === id);
}

export function settingsGroupLabel(id: string): string {
  const category = settingsCategories.find(category => category.id !== 'runtime' && (category.groups as readonly string[]).includes(id));
  const labels: Record<string, string> = {
    tooling: 'settings.editor.groupTools', mcp: 'settings.editor.groupMcp', resources: 'settings.editor.groupResources',
    agents: 'settings.editor.groupAgents', default_agent: 'settings.editor.groupDefaultAgent', workspaces: 'settings.editor.groupConnections',
    logging: 'settings.editor.groupLogging', telemetry: 'settings.editor.groupTelemetry', statistics: 'settings.editor.groupStatistics',
    runtime: 'settings.category.runtime', development: 'settings.category.development', context: 'settings.category.context',
    ui: 'settings.category.conversation', features: 'settings.category.features', permissions: 'settings.category.permissions',
  };
  return labels[id] ? t(labels[id]) : category ? t(category.label) : id;
}
