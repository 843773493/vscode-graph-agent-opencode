import { startWorkbench } from './startWorkbench.js';
import { commands } from '../upstream/src/vs/workbench/workbench.web.main.internal.js';
import { CommandsRegistry } from '../upstream/src/vs/platform/commands/common/commands.js';
import { IStorageService, WillSaveStateReason } from '../upstream/src/vs/platform/storage/common/storage.js';
import { threadHistoryCache } from '../../web/src/state/threadHistoryCache.js';

CommandsRegistry.registerCommand('boxteam.flushViewState', accessor => accessor.get(IStorageService).flush(WillSaveStateReason.SHUTDOWN));
// Web 与桌面共用同一生命周期；原生宿主只等待既有前端存储提交。
Object.defineProperty(globalThis, 'boxteamWorkbenchLifecycle', {
  value: Object.freeze({ prepareToClose: async () => {
    await threadHistoryCache.flushAll();
    await commands.executeCommand('boxteam.flushViewState');
  } }),
  writable: false,
});

await startWorkbench();
