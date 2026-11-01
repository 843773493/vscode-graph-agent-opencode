import { create, URI, commands } from '../upstream/src/vs/workbench/workbench.web.main.internal.js';
import { isCancellationError } from '../upstream/src/vs/base/common/errors.js';
import { workspaceFolder } from './filesystem.js';
import './thread.contribution.js';

export async function startWorkbench(): Promise<void> {
  function showError(error: unknown): void {
    // 与原版工作台一致：视图释放时取消异步任务属于正常控制流。
    if (isCancellationError(error)) return;
    const display = document.getElementById('startup-error');
    if (display) display.textContent = error instanceof Error ? error.stack ?? error.message : String(error);
  }
  window.addEventListener('error', event => showError(event.error ?? event.message));
  window.addEventListener('unhandledrejection', event => showError(event.reason));

  const parameters = new URLSearchParams(location.search);
  await import('./agent.contribution.js');
  const folder = parameters.get('folder');
  const workspaceFile = parameters.get('workspace');
  const workspace = workspaceFile ? { workspaceUri: URI.parse(workspaceFile) }
    : { folderUri: folder ? URI.parse(folder) : workspaceFolder };
  const container = document.getElementById('workbench');
  if (!container) throw new Error('缺少 Workbench 容器');
  create(container, {
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
