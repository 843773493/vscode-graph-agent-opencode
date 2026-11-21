import { Action2, MenuId, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { getGatewayHealth, restartDevelopmentRuntime } from '../../../web/src/gatewayApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { createManagementTargetLifecycle, managementGateway, listManagementTargets, type ManagementTarget } from '../managementTarget.js';
import type { GatewayDiagnosticLogLocateRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { t } from '../../../shared/core/i18n/index.js';
import messages from '../../../shared/core/i18n/messages/en.json';

for (const [page, title] of [['connections', 'gateway.control.connections'], ['credentials', 'gateway.control.connectionsAndCredentials'],
  ['runtime', 'gateway.control.runtime'], ['diagnostics', 'gateway.control.diagnostics']] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.gateway.' + page, title: { value: t(title), original: messages[title] }, f1: true,
    menu: page === 'credentials' ? [{ id: MenuId.AccountsContext, group: '2_boxteam', order: 1 }] : undefined }); }
  async run(accessor: ServicesAccessor, explicit?: ManagementTarget | string, workspaceId?: string, diagnosticLocation?: GatewayDiagnosticLogLocateRequest): Promise<void> {
    const commands = accessor.get(ICommandService);
    await workspaceContext.ensureReady();
    const entry = workspaceContext.getSnapshot().gatewayId!;
    // 连接目录及入站设备属于入口控制面；诊断和运行服务才接受指定目标。
    let target = managementGateway('settings', typeof explicit === 'string' ? explicit : explicit?.gateway_id ?? entry);
    if (typeof explicit === 'object') target = { ...explicit };
    if (page === 'connections' || page === 'credentials') {
      if (explicit && target.gateway_id !== entry) throw new Error(t('workbench.workspaceAction.identityChanged'));
      target = managementGateway('settings', entry);
    }
    await commands.executeCommand('boxteam.settings.openTarget', target, { workspace_id: workspaceId, source_layer: 'user' },
      { settingsCategory: page === 'connections' || page === 'credentials' ? 'connections' : page === 'runtime' ? 'runtime' : 'observability',
        managementPage: page, diagnosticLocation });
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.gateway.menu', title: { value: t('gateway.control.connections'), original: 'Remote Connections' }, f1: false }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const quick = accessor.get(IQuickInputService), commands = accessor.get(ICommandService);
    await workspaceContext.ensureReady();
    const lifetime = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
    try {
      const targets = await listManagementTargets(lifetime.signal); lifetime.check();
      const choices = [
        { label: '$(plug) ' + t('gateway.control.connections'), command: 'boxteam.gateway.connections', target: undefined as ManagementTarget | undefined },
        { label: '$(shield) ' + t('gateway.control.connectionsAndCredentials'), command: 'boxteam.gateway.credentials', target: undefined },
        { label: '$(pulse) ' + t('gateway.control.runtime'), command: 'boxteam.gateway.runtime', target: undefined },
        { label: '$(output) ' + t('gateway.control.diagnostics'), command: 'boxteam.gateway.diagnostics', target: undefined },
        ...targets.filter(item => item.connection_kind === 'remote_gateway').map(target => ({ label: '$(remote) ' + target.name,
          description: target.connection_error ?? '', command: 'boxteam.gateway.diagnostics', target })),
      ];
      const selected = await quick.pick(choices, { title: t('gateway.control.connections'), placeHolder: t('gateway.control.connectionsDescription') }, lifetime.token);
      lifetime.check();
      if (selected) await commands.executeCommand(selected.command, selected.target);
    } finally { lifetime.dispose(); }
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.gateway.restartDevelopment', title: { value: t('gateway.control.restartLocal'), original: 'Restart Local Development Environment' }, f1: true }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const dialogs = accessor.get(IDialogService), progress = accessor.get(IProgressService);
    const accepted = await dialogs.confirm({ type: 'warning', message: t('gateway.control.restartTitle'), detail: t('gateway.control.restartDetail'), primaryButton: t('gateway.control.restartConfirm') });
    if (!accepted.confirmed) return;
    const health = await getGatewayHealth(0);
    if (!health.development_restart_available) throw new Error(t('gateway.settings.restartUnavailable'));
    await progress.withProgress({ location: ProgressLocation.Notification, title: t('gateway.control.restarting') }, async () => {
      const previous = await restartDevelopmentRuntime(0), deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 500));
        let next;
        try { next = await getGatewayHealth(0); }
        catch { continue; } // 重启期间连接中断是预期状态；期限结束仍明确失败。
        if (next.process_id !== previous.previous_process_id) { window.location.reload(); return; }
      }
      throw new Error(t('gateway.control.restartTimedOut', 120));
    });
  }
});
