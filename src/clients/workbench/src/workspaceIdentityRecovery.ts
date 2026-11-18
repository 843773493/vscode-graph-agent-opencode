import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { t, formatNumber } from '../../shared/core/i18n/index.js';
import { Action2, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IStorageService, StorageScope, StorageTarget, WillSaveStateReason } from '../upstream/src/vs/platform/storage/common/storage.js';
import { browserGatewayId } from '../../web/src/hooks/settings/browserUiSettings.js';
import { browserViewKey, updateBrowserView } from '../../web/src/state/browserViewStore.js';
import { recoverWorkspaceIdentity } from '../../web/src/state/workspaceIdentityMigration/recovery.js';
import { parseWorkspaceIdentityRecoveries, recoverThreadEditor } from '../../web/src/state/workspaceIdentityMigration/material.js';
import { ThreadSerializer, UnavailableThreadInput } from './threadInput.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IDialogService } from '../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IHostService } from '../upstream/src/vs/workbench/services/host/browser/host.js';
import { errorMessage } from '../../web/src/utils/errorMessage.js';

async function selectRecoveryFiles(): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file'; input.accept = '.json,application/json'; input.multiple = true;
    input.oncancel = () => resolve(undefined);
    input.onchange = () => {
      const files = Array.from(input.files ?? []);
      if (!files.length) { resolve(undefined); return; }
      void Promise.all(files.map(async file => JSON.parse(await file.text()) as unknown)).then(values => resolve(values.flat()), reject);
    };
    input.click();
  });
}

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.workspaceIdentity.importRecovery', title: { value: t('workbench.identityRecovery.import'), original: actionTitles['workbench.identityRecovery.import'] }, f1: true }); }
  async run(accessor: ServicesAccessor, input?: unknown, options?: { automatic?: boolean }): Promise<unknown> {
    const groups = accessor.get(IEditorGroupsService);
    const storage = accessor.get(IStorageService), instantiation = accessor.get(IInstantiationService);
    const dialogs = accessor.get(IDialogService), host = accessor.get(IHostService);
    const source = input ?? await selectRecoveryFiles();
    if (source === undefined) return;
    const materials = parseWorkspaceIdentityRecoveries(typeof source === 'string' ? JSON.parse(source) : source);
    const connectedGateway = await browserGatewayId(0);
    if (!materials.some(material => material.registry_gateway_id === connectedGateway)) throw new Error(t('workbench.identityRecovery.gatewayMissing'));
    const report = await recoverWorkspaceIdentity(materials);
    const editors = { storage: 'workbench-editors', changed: 0, issues: [] as { key: string; message: string }[] };
    for (const group of groups.groups) {
      for (const editor of group.editors) {
        if (!(editor instanceof UnavailableThreadInput)) continue;
        try {
          const recovered = recoverThreadEditor(editor.stored, materials);
          const replacement = new ThreadSerializer().deserialize(instantiation, JSON.stringify(recovered));
          await group.replaceEditors([{ editor, replacement, options: { preserveFocus: true } }]);
          editors.changed++;
        } catch (error) { editors.issues.push({ key: `${group.id}:${editor.getName()}`, message: error instanceof Error ? error.message : String(error) }); }
      }
    }
    // 无 Gateway 来源的旧面板键保留，不因当前连接恰有同名 alias 而改投。
    for (const key of storage.keys(StorageScope.WORKSPACE, StorageTarget.MACHINE)) {
      if (key.startsWith('boxteam.workspacePanel.gw_')) editors.issues.push({ key, message: t('workbench.identityRecovery.oldPanel') });
    }
    report.batches.push(editors);
    try { await storage.flush(WillSaveStateReason.NONE); }
    catch (error) { editors.issues.push({ key: 'workbench-storage', message: errorMessage(error) }); }
    try {
      for (const material of materials) await updateBrowserView(browserViewKey(material.registry_gateway_id, 'workspace-identity-recovery', material.migration_id), () => report);
    } catch (error) {
      report.batches.push({ storage: 'recovery-report', changed: 0,
        issues: [{ key: 'report', message: t('localData.reportFailure', errorMessage(error)) }] });
    }
    const issues = report.batches.flatMap(batch => batch.issues.map(issue => `${batch.storage} / ${issue.key}：${issue.message}`));
    if (options?.automatic) {
      if (!issues.length) await host.reload();
      return report;
    }
    const { result } = await dialogs.prompt({ type: issues.length ? 'warning' : 'info',
      message: t('localData.recoveryReport'), detail: [
        ...report.batches.map(batch => t('localData.recoveredBatch', batch.storage, formatNumber(batch.changed))),
        issues.length ? t('workbench.identityRecovery.incomplete', formatNumber(issues.length), issues.join('\n')) : t('workbench.identityRecovery.saved'),
      ].join('\n\n'), buttons: [{ label: t('localData.reload'), run: () => true }], cancelButton: true });
    if (result) await host.reload();
    return report;
  }
});
