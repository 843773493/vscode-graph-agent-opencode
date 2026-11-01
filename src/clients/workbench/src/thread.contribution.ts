import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { SyncDescriptor } from '../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { EditorPaneDescriptor, IEditorPaneRegistry } from '../upstream/src/vs/workbench/browser/editor.js';
import { EditorExtensions, IEditorFactoryRegistry } from '../upstream/src/vs/workbench/common/editor.js';
import { ThreadInput, ThreadSerializer } from './threadInput.js';
import { ThreadPane } from './threadPane.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { IEditorResolverService, RegisteredEditorPriority } from '../upstream/src/vs/workbench/services/editor/common/editorResolverService.js';

Registry.as<IEditorPaneRegistry>(EditorExtensions.EditorPane).registerEditorPane(
  EditorPaneDescriptor.create(ThreadPane, ThreadPane.ID, 'Thread'), [new SyncDescriptor(ThreadInput)],
);
Registry.as<IEditorFactoryRegistry>(EditorExtensions.EditorFactory).registerEditorSerializer(ThreadInput.ID, ThreadSerializer);

/** 通过原版解析器恢复关闭、历史导航及拖拽产生的资源输入。 */
class ThreadEditorContribution extends Disposable {
  constructor(@IEditorResolverService resolver: IEditorResolverService) {
    super();
    this._register(resolver.registerEditor('*', {
      id: ThreadInput.ID, label: 'Thread', priority: RegisteredEditorPriority.exclusive,
    }, {
      canSupportResource: resource => resource.scheme === 'boxteam-thread',
    }, {
      createEditorInput: input => {
        const [sessionId, threadId, extra] = input.resource.path.slice(1).split('/');
        const routeId = new URLSearchParams(input.resource.query).get('route');
        if (!routeId || !input.resource.authority || !sessionId || !threadId || extra !== undefined) {
          throw new Error('Thread 编辑器资源缺少精确工作区、Session 或 Thread');
        }
        const editor = new ThreadInput({ routeId, workspaceId: input.resource.authority, sessionId, threadId,
          title: input.label ?? threadId });
        editor.restoreViewState(input.options?.viewState);
        return { editor, options: input.options };
      },
    }));
  }
}
registerWorkbenchContribution2('boxteam.threadEditor', ThreadEditorContribution, WorkbenchPhase.BlockRestore);
