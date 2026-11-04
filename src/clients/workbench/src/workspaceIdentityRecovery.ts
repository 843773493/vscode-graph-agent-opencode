import { Action2, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IStorageService, StorageScope, StorageTarget, WillSaveStateReason } from '../upstream/src/vs/platform/storage/common/storage.js';
import { browserGatewayId } from '../../web/src/hooks/settings/browserUiSettings.js';
import { browserViewKey, updateBrowserView } from '../../web/src/state/browserViewStore.js';
import { recoverWorkspaceIdentity } from '../../web/src/state/workspaceIdentityMigration/recovery.js';
import { parseWorkspaceIdentityRecoveries, recoverThreadEditor } from '../../web/src/state/workspaceIdentityMigration/material.js';
import { ThreadSerializer, UnavailableThreadInput } from './threadInput.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';

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
  constructor() { super({ id: 'boxteam.workspaceIdentity.importRecovery', title: { value: '导入工作区身份恢复材料', original: 'Import Workspace Identity Recovery' }, f1: true }); }
  async run(accessor: ServicesAccessor, input?: unknown): Promise<unknown> {
    const notifications = accessor.get(INotificationService), groups = accessor.get(IEditorGroupsService);
    const storage = accessor.get(IStorageService), instantiation = accessor.get(IInstantiationService);
    const source = input ?? await selectRecoveryFiles();
    if (source === undefined) return;
    const materials = parseWorkspaceIdentityRecoveries(typeof source === 'string' ? JSON.parse(source) : source);
    const connectedGateway = await browserGatewayId(0);
    if (!materials.some(material => material.registry_gateway_id === connectedGateway)) throw new Error('恢复材料未包含当前连接的登记 Gateway，原记录未改动');
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
      if (key.startsWith('boxteam.workspacePanel.gw_')) editors.issues.push({ key, message: '旧面板记录缺少来源 Gateway，原记录已保留' });
    }
    report.batches.push(editors);
    await storage.flush(WillSaveStateReason.NONE);
    for (const material of materials) await updateBrowserView(browserViewKey(material.registry_gateway_id, 'workspace-identity-recovery', material.migration_id), () => report);
    const issues = report.batches.flatMap(batch => batch.issues.map(issue => `${batch.storage} / ${issue.key}：${issue.message}`));
    if (issues.length) notifications.error(`工作区身份恢复有 ${issues.length} 项未完成，原件保留，可重试同一材料：\n${issues.join('\n')}`);
    else notifications.info('工作区身份恢复已保存，请重载工作台以恢复选择和待确认操作。');
    return report;
  }
});
