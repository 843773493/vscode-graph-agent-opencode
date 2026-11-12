import { Action2, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { t } from '../../shared/core/i18n/index.js';
import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { IDialogService } from '../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { threadHistoryCache } from '../../web/src/state/threadHistoryCache.js';
import { ThreadInput } from './threadInput.js';

registerAction2(class ClearThreadHistoryCache extends Action2 {
  constructor() {
    super({ id: 'boxteam.thread.clearHistoryCache',
      title: { value: t('workbench.thread.clearHistoryCache'), original: actionTitles['workbench.thread.clearHistoryCache'] }, f1: true });
  }

  async run(accessor: ServicesAccessor): Promise<void> {
    const input = accessor.get(IEditorService).activeEditor;
    if (!(input instanceof ThreadInput)) throw new Error(t('thread.cache.activateFirst'));
    // 确认期间即使焦点切换，也只清理发起时的精确 owner，不重解路由或启动目标。
    const { gatewayId, workspaceId, sessionId, threadId } = input.owner;
    const owner = { gatewayId, workspaceId, sessionId, threadId };
    const dialogs = accessor.get(IDialogService), notifications = accessor.get(INotificationService);
    const confirmation = await dialogs.confirm({ type: 'question', message: t('thread.cache.clearConfirm'),
      detail: t('thread.cache.clearDetail', gatewayId, workspaceId, sessionId, threadId),
      primaryButton: t('thread.cache.clearButton'), cancelButton: t('dialogs.cancel') });
    if (!confirmation.confirmed) return;
    await threadHistoryCache.remove(owner);
    notifications.info(t('thread.cache.cleared', threadId));
  }
});
