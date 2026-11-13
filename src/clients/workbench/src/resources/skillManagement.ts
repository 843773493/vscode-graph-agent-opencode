import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import type { SkillSourcesRefreshDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/skill_sources.js';
import { gatewayWorkspaceKey, gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { ConfigurationSourceOwner } from '../settings/sourceOwner.js';
import { openConfigurationSourceCommand } from '../settings/commands.js';
import type { TargetSettingsLocation } from '../settings/scope.js';
import { createManagementTargetLifecycle } from '../managementTarget.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';

type ActionKey = 'workbench.skills.openGatewaySources' | 'workbench.skills.openWorkspaceSources'
  | 'workbench.skills.refreshGatewaySources' | 'workbench.skills.refreshWorkspaceSources';

interface SkillAction {
  id: string;
  titleKey: ActionKey;
  run(accessor: ServicesAccessor): Promise<void>;
}

function openSources(workspace: boolean): SkillAction['run'] {
  return async accessor => {
    const quick = accessor.get(IQuickInputService);
    const commands = accessor.get(ICommandService);
    const ready = await workspaceContext.ensureReady();
    const lifecycle = createManagementTargetLifecycle(t('workbench.skills.identityChanged'));
    try {
      const current = workspaceContext.getSnapshot();
      if (current.error) throw new Error(current.error);
      if (ready.gatewayId !== lifecycle.entryGatewayId || current.loading || current.gatewayId !== lifecycle.entryGatewayId) throw new Error(t('workbench.skills.gatewayNotReady'));
      let target: { gateway_id: string; workspace_id?: string; name: string };
      if (workspace) {
        const chosen = await quick.pick(current.workspaces.filter(item => item.gateway_id === lifecycle.entryGatewayId).map(item => ({
          label: item.name, description: item.status, detail: item.workspace_id, owner: gatewayWorkspaceOwner(item), name: item.name,
        })), { title: t('settings.editor.chooseTarget'), placeHolder: t('settings.editor.sourceAccessBoundary') });
        lifecycle.check();
        if (!chosen) return;
        const resolved = requireGatewayWorkspaceRequestTarget(current.workspaces, chosen.owner);
        if (resolved.status !== 'ready') throw new Error(t('workbench.skills.workspaceUnavailable', resolved.name, resolved.status));
        target = { gateway_id: resolved.gateway_id, workspace_id: resolved.workspace_id, name: resolved.name };
      } else {
        target = { gateway_id: lifecycle.entryGatewayId, name: t('workbench.managementTarget.local') };
      }
      const owner = new ConfigurationSourceOwner(target);
      const listed = await owner.list(lifecycle.signal);
      lifecycle.check();
      const sources = listed.sources.filter(source => source.source_key);
      if (!sources.length) throw new Error(t('workbench.skills.noConfigSources'));
      const selected = await quick.pick(sources.map(source => ({
        label: source.source_key!,
        description: t('settings.editor.layerPresencePriority', source.layer, source.presence, source.precedence),
        detail: source.uri ?? undefined,
        sourceKey: source.source_key!,
      })), { title: t('settings.editor.chooseFormalSource'), prompt: `${target.gateway_id} · ${target.name}`,
        placeHolder: t('settings.editor.sourceAccessBoundary') });
      lifecycle.check();
      if (!selected) return;
      const location: TargetSettingsLocation = { version: 2, ...owner.target, source_key: selected.sourceKey, name: target.name };
      await commands.executeCommand(openConfigurationSourceCommand, { location });
      lifecycle.check();
    } finally {
      lifecycle.dispose();
    }
  };
}

async function chooseWorkspaceOwner(quick: IQuickInputService, lifecycle: ReturnType<typeof createManagementTargetLifecycle>): Promise<WorkspaceOwner | undefined> {
  const current = workspaceContext.getSnapshot();
  if (current.error) throw new Error(current.error);
  if (current.loading || current.gatewayId !== lifecycle.entryGatewayId) throw new Error(t('workbench.skills.gatewayNotReady'));
  const owner = (await quick.pick(current.workspaces.filter(item => item.gateway_id === lifecycle.entryGatewayId).map(item => ({
    label: item.name, description: item.status, detail: item.workspace_id, owner: gatewayWorkspaceOwner(item),
  })), { title: t('settings.editor.chooseTarget'), placeHolder: t('settings.editor.sourceAccessBoundary') }))?.owner;
  lifecycle.check();
  if (!owner) return;
  const target = requireGatewayWorkspaceRequestTarget(current.workspaces, owner);
  if (target.status !== 'ready') throw new Error(t('workbench.skills.workspaceUnavailable', target.name, target.status));
  return { gatewayId: target.gateway_id, workspaceId: target.workspace_id };
}

const actions: SkillAction[] = [
  { id: 'boxteam.skills.openGatewaySources', titleKey: 'workbench.skills.openGatewaySources', run: openSources(false) },
  { id: 'boxteam.skills.openWorkspaceSources', titleKey: 'workbench.skills.openWorkspaceSources', run: openSources(true) },
  {
    id: 'boxteam.skills.refreshGatewaySources', titleKey: 'workbench.skills.refreshGatewaySources',
    async run(accessor) {
      const notifications = accessor.get(INotificationService);
      await workspaceContext.ensureReady();
      const lifecycle = createManagementTargetLifecycle(t('workbench.skills.identityChanged'));
      try {
        const result = await gateway.request<{ gateway_id: string; revision: string; available: boolean }>(
          '/api/gateway/skill-sources/refresh', { method: 'POST', signal: lifecycle.signal });
        lifecycle.check();
        if (result.gateway_id !== lifecycle.entryGatewayId) throw new Error(t('workbench.skills.gatewayOwnerMismatch'));
        notifications.info(t('workbench.skills.refreshGatewayComplete', result.available ? t('workbench.skills.available') : t('workbench.skills.unavailable')));
      } finally {
        lifecycle.dispose();
      }
    },
  },
  {
    id: 'boxteam.skills.refreshWorkspaceSources', titleKey: 'workbench.skills.refreshWorkspaceSources',
    async run(accessor) {
      const quick = accessor.get(IQuickInputService);
      const notifications = accessor.get(INotificationService);
      await workspaceContext.ensureReady();
      const lifecycle = createManagementTargetLifecycle(t('workbench.skills.identityChanged'));
      try {
        const owner = await chooseWorkspaceOwner(quick, lifecycle);
        if (!owner) return;
        const result = await gateway.request<SkillSourcesRefreshDTO>('/api/v1/resources/skill-sources/refresh', {
          method: 'POST', signal: lifecycle.signal,
        }, gatewayWorkspaceKey(owner));
        lifecycle.check();
        if (result.workspace_id !== owner.workspaceId) throw new Error(t('workbench.skills.workspaceOwnerMismatch'));
        notifications.info(t('workbench.skills.refreshWorkspaceComplete', result.available ? t('workbench.skills.available') : t('workbench.skills.unavailable')));
      } finally {
        lifecycle.dispose();
      }
    },
  },
];

for (const action of actions) registerAction2(class extends Action2 {
  constructor() {
    super({ id: action.id, title: { value: t(action.titleKey), original: actionTitles[action.titleKey] }, f1: true });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const notifications = accessor.get(INotificationService);
    try {
      await action.run(accessor);
    } catch (cause) {
      notifications.error(cause instanceof Error ? cause : String(cause));
    }
  }
});
