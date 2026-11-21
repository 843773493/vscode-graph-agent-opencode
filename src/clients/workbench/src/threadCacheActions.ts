import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { t } from '../../shared/core/i18n/index.js';
import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { IDialogService } from '../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { formatByteSize, formatLocalTimestamp } from '../../web/src/utils/format.js';
import { threadHistoryCache, type ThreadHistoryOwner } from '../../web/src/state/threadHistoryCache.js';
import { ThreadInput } from './threadInput.js';

async function clearHistoryCache(owner: ThreadHistoryOwner, dialogs: IDialogService, notifications: INotificationService): Promise<void> {
  const { gatewayId, workspaceId, sessionId, threadId } = owner;
  const confirmation = await dialogs.confirm({ type: 'question', message: t('thread.cache.clearConfirm'),
    detail: t('thread.cache.clearDetail', gatewayId, workspaceId, sessionId, threadId),
    primaryButton: t('thread.cache.clearButton'), cancelButton: t('dialogs.cancel') });
  if (!confirmation.confirmed) return;
  await threadHistoryCache.remove(owner);
  notifications.info(t('thread.cache.cleared', threadId));
}

for (const operation of ['inspect', 'clear'] as const) registerAction2(class extends Action2 {
  constructor() {
    const key = operation === 'inspect' ? 'workbench.thread.inspectHistoryCache' : 'workbench.thread.clearHistoryCache';
    super({ id: operation === 'inspect' ? 'boxteam.thread.inspectHistoryCache' : 'boxteam.thread.clearHistoryCache',
      title: { value: t(key), original: actionTitles[key] }, f1: true,
      menu: { id: MenuId.EditorTitleContext, group: '2_boxteam_cache', order: operation === 'inspect' ? 0 : 1,
        when: ContextKeyExpr.equals('activeEditor', ThreadInput.ID) } });
  }

  async run(accessor: ServicesAccessor): Promise<void> {
    const input = accessor.get(IEditorService).activeEditor;
    if (!(input instanceof ThreadInput)) throw new Error(t('thread.cache.activateFirst'));
    // 确认期间即使焦点切换，也只清理发起时的精确 owner，不重解路由或启动目标。
    const { gatewayId, workspaceId, sessionId, threadId } = input.owner;
    const owner = { gatewayId, workspaceId, sessionId, threadId };
    const dialogs = accessor.get(IDialogService), notifications = accessor.get(INotificationService);
    if (operation === 'clear') { await clearHistoryCache(owner, dialogs, notifications); return; }
    const information = await threadHistoryCache.inspect(owner);
    const snapshot = (value: { bytes: number; savedAt: number } | null) => value
      ? t('thread.cache.snapshot', formatByteSize(value.bytes), formatLocalTimestamp(new Date(value.savedAt).toISOString()))
      : t('thread.cache.absent');
    const choice = await dialogs.prompt({ type: 'info', message: t('workbench.thread.inspectHistoryCache'),
      detail: t('thread.cache.information', gatewayId, workspaceId, sessionId, threadId,
        snapshot(information.persisted), snapshot(information.memory),
        t(information.pending ? 'thread.cache.pending' : 'thread.cache.noPending'),
        information.maximumEntries, formatByteSize(information.maximumBytes)),
      buttons: [{ label: t('thread.cache.clearButton'), run: () => true }], cancelButton: t('dialogs.close') });
    if (choice.result) await clearHistoryCache(owner, dialogs, notifications);
  }
});
