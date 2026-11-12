import { Action2, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { IDialogService } from '../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { threadHistoryCache } from '../../web/src/state/threadHistoryCache.js';
import { ThreadInput } from './threadInput.js';

registerAction2(class ClearThreadHistoryCache extends Action2 {
  constructor() {
    super({ id: 'boxteam.thread.clearHistoryCache',
      title: { value: '清除当前 Thread 历史展示缓存', original: 'Clear Current Thread History Display Cache' }, f1: true });
  }

  async run(accessor: ServicesAccessor): Promise<void> {
    const input = accessor.get(IEditorService).activeEditor;
    if (!(input instanceof ThreadInput)) throw new Error('请先激活需要清理历史展示缓存的 Thread');
    // 确认期间即使焦点切换，也只清理发起时的精确 owner，不重解路由或启动目标。
    const { gatewayId, workspaceId, sessionId, threadId } = input.owner;
    const owner = { gatewayId, workspaceId, sessionId, threadId };
    const dialogs = accessor.get(IDialogService), notifications = accessor.get(INotificationService);
    const confirmation = await dialogs.confirm({ type: 'question', message: '清除当前 Thread 历史展示缓存？',
      detail: `Gateway：${gatewayId}\n工作区：${workspaceId}\nSession：${sessionId}\nThread：${threadId}\n\n只删除本机的历史展示镜像，后续同步可重新生成。消息、草稿、设置、最近记录和编辑器恢复数据保持不变。`,
      primaryButton: '清除展示缓存', cancelButton: '取消' });
    if (!confirmation.confirmed) return;
    await threadHistoryCache.remove(owner);
    notifications.info(`已清除 Thread ${threadId} 的历史展示缓存`);
  }
});
