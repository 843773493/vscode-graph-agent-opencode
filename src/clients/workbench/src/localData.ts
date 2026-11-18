import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { t } from '../../shared/core/i18n/index.js';
import { Action } from '../upstream/src/vs/base/common/actions.js';
import { Event } from '../upstream/src/vs/base/common/event.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { IDialogService } from '../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService, Severity, type INotificationHandle } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { IHostService } from '../upstream/src/vs/workbench/services/host/browser/host.js';
import { LOCAL_DATABASES, LocalDatabaseError, inspectLocalDatabases, setLocalDatabaseReporter } from '../../web/src/state/browserPersistence.js';
import { openBrowserViewDatabase } from '../../web/src/state/browserViewStore.js';
import { openCatalogOutboxDatabase } from '../../web/src/state/session/sessionCatalogOutboxStore.js';
import { openWorkspaceFileIntentStore } from '../../web/src/state/workspaceFiles/intentStore.js';
import { errorMessage, isErrorReported } from '../../web/src/utils/errorMessage.js';
import { getGatewayClientRecovery } from '../../web/src/api/gateway/clientRecovery.js';
import type { WorkspaceIdentityRecoveryReport } from '../../web/src/state/workspaceIdentityMigration/recovery.js';

const commandId = 'boxteam.localData.manage';

/** 只有宿主汇总数据库通知；业务 owner 仍持有原意图与持久化事务。 */
class LocalDataContribution extends Disposable {
  constructor(@INotificationService notifications: INotificationService, @ICommandService commands: ICommandService) {
    super();
    const shown = new Map<string, { message: string; error: LocalDatabaseError; handle: INotificationHandle }>();
    let attempted = false, recovering = false, disposed = false;
    const manage = this._register(new Action(commandId, t('localData.manage'), '', true, () => commands.executeCommand(commandId)));
    const show = (error: LocalDatabaseError, progress = false) => {
      const definition = Object.values(LOCAL_DATABASES).find(item => item.name === error.database);
      const label = definition ? t(definition.label) : error.database;
      const message = progress ? t('localData.autoRecovering', label) : t('localData.failure', label, error.message);
      const previous = shown.get(error.database);
      if (previous?.message === message) return;
      previous?.handle.close();
      const handle = notifications.notify({ id: `${commandId}.${error.database}`, severity: progress ? Severity.Info : Severity.Error, message,
        actions: { primary: [manage] } });
      shown.set(error.database, { message, error, handle });
      Event.once(handle.onDidClose)(() => { if (shown.get(error.database)?.handle === handle) shown.delete(error.database); });
    };
    const recover = async (initial: LocalDatabaseError) => {
      try {
        const receipt = await getGatewayClientRecovery(0);
        if (disposed) return;
        if (!receipt.workspace_identity) throw new Error(t('localData.noRecoveryReceipt'));
        const report = (await commands.executeCommand<WorkspaceIdentityRecoveryReport>('boxteam.workspaceIdentity.importRecovery', receipt.workspace_identity, { automatic: true }))!;
        const issues = report.batches.flatMap(batch => batch.issues.map(issue => `${batch.storage} / ${issue.key}：${issue.message}`));
        if (issues.length) throw new Error(issues.join('\n'));
      } catch (cause) {
        if (!disposed) {
          // 恢复事务自身的 AbortError 可能已替换通知，最终结果仍须交付最初的身份故障。
          const others = [...shown.values()].map(entry => entry.error).filter(error => error.kind === 'identity' && error.database !== initial.database);
          for (const error of [initial, ...others]) show(new LocalDatabaseError(error.database, 'identity', new Error(t('localData.autoRecoveryFailed', error.message, errorMessage(cause)), { cause })));
        }
      } finally { recovering = false; }
    };
    this._register({ dispose: setLocalDatabaseReporter(error => {
      if (error.kind !== 'identity' || attempted && !recovering) { show(error); return; }
      show(error, true);
      if (!attempted) { attempted = true; recovering = true; void recover(error); }
    }) });
    this._register({ dispose: () => { disposed = true; for (const entry of shown.values()) entry.handle.close(); } });
  }
}
registerWorkbenchContribution2('boxteam.localData', LocalDataContribution, WorkbenchPhase.BlockRestore);

registerAction2(class extends Action2 {
  constructor() {
    super({ id: commandId, title: { value: t('localData.manage'), original: actionTitles['localData.manage'] }, f1: true,
      menu: [ { id: MenuId.GlobalActivity, group: '2_configuration', order: 9 },
        { id: MenuId.MenubarPreferencesMenu, group: '2_configuration', order: 9 } ] });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const quick = accessor.get(IQuickInputService), dialogs = accessor.get(IDialogService);
    const commands = accessor.get(ICommandService), host = accessor.get(IHostService), notifications = accessor.get(INotificationService);
    const databases = await inspectLocalDatabases();
    const selected = await quick.pick([
      ...databases.map(database => ({ label: t(database.label), description: database.storedVersion === undefined
        ? t('localData.notCreated') : t('localData.version', database.storedVersion, database.version),
        detail: database.error ?? (Object.keys(database.stores).length
          ? t('localData.counts', Object.entries(database.stores).map(([store, count]) => `${store}: ${count}`).join(' · ')) : undefined), database })),
      { label: t('workbench.identityRecovery.import'), description: t('localData.importHint'), database: undefined },
    ], { title: t('localData.manage'), placeHolder: t('localData.choose') });
    if (!selected) return;
    if (!selected.database) { await commands.executeCommand('boxteam.workspaceIdentity.importRecovery'); return; }
    const database = selected.database;
    const { result } = await dialogs.prompt({ type: database.error ? 'error' : 'info', message: t(database.label),
      detail: [database.name, selected.description, selected.detail, t('localData.boundary')].join('\n\n'),
      buttons: [ { label: t('localData.retry'), run: () => 'retry' },
        { label: t('workbench.identityRecovery.import'), run: () => 'import' },
        { label: t('localData.reload'), run: () => 'reload' } ], cancelButton: true });
    if (result === 'import') await commands.executeCommand('boxteam.workspaceIdentity.importRecovery');
    if (result === 'reload') await host.reload();
    if (result === 'retry') {
      try {
        if (database.name === LOCAL_DATABASES.catalog.name) (await openCatalogOutboxDatabase()).close();
        else if (database.name === LOCAL_DATABASES.files.name) await openWorkspaceFileIntentStore();
        else await openBrowserViewDatabase();
      } catch (error) { if (isErrorReported(error)) return; throw error; }
      notifications.prompt(Severity.Info, t('localData.connected'), [ { label: t('localData.reload'), run: () => host.reload() } ]);
    }
  }
});
