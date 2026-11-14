import { create, URI, commands } from '../upstream/src/vs/workbench/workbench.web.main.internal.js';
import { workspaceFolder } from './filesystem.js';
import './thread.contribution.js';
import './resourceOpener.js';
import './composerActions.js';
import './changeFiles.js';
import { installReadonlyDocumentSerialization } from './readonlyDocuments.js';
import './readonlyDocuments.contribution.js';
import './search/nameModes.js';
import './search/workspaceQuickAccess.js';
import './automation/contribution.js';
import './resources/quickAccess.js';
import './resources/skillManagement.js';
import './extensions/contribution.js';
import './extensions/pluginCommands.js';
import './debug/contribution.js';
import './managementTarget.js';
import './theme.js';

declare const __BOXTEAM_PRODUCT__: { nameShort: string; nameLong: string; version: string; commit: string; date: string };

installReadonlyDocumentSerialization();

export async function startWorkbench(): Promise<void> {
  const parameters = new URLSearchParams(location.search);
  await import('./agent.contribution.js');
  const folder = parameters.get('folder');
  const workspaceFile = parameters.get('workspace');
  const workspace = workspaceFile ? { workspaceUri: URI.parse(workspaceFile) }
    : { folderUri: folder ? URI.parse(folder) : workspaceFolder };
  const container = document.getElementById('workbench');
  if (!container) throw new Error('缺少 Workbench 容器');
  create(container, {
    productConfiguration: { nameShort: __BOXTEAM_PRODUCT__.nameShort, nameLong: __BOXTEAM_PRODUCT__.nameLong },
    // 避开底部 Composer，防止错误通知在消失前阻挡输入和发送。
    configurationDefaults: { 'workbench.notifications.position': 'top-right' },
    // TODO：辅助侧边栏的 Agent 内容待后续设计；会话资源只放入对应 Thread 编辑器。
    workspaceProvider: {
      workspace, trusted: folder || workspaceFile ? undefined : true,
      async open(next, options) {
        const target = new URL(location.href);
        target.searchParams.delete('folder');
        target.searchParams.delete('workspace');
        if (next && 'folderUri' in next) target.searchParams.set('folder', next.folderUri.toString());
        if (next && 'workspaceUri' in next) target.searchParams.set('workspace', next.workspaceUri.toString());
        if (options?.reuse) { location.assign(target.href); return true; }
        return window.open(target.href) !== null;
      },
    },
  });
  // 仅暴露原版嵌入 API，供宿主及真实浏览器验收使用。
  Object.assign(globalThis, { workbench: { commands, URI } });
}
