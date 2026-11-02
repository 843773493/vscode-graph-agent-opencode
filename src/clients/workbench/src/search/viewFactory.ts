import { toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { IViewletViewOptions } from '../../upstream/src/vs/workbench/browser/parts/views/viewsViewlet.js';
import type { IViewDescriptor } from '../../upstream/src/vs/workbench/common/views.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { VIEW_ID } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { SearchWorkspaceContext } from './scope.js';
import { captureSearchOwner, loadSearchScope } from './target.js';
import type { ScopedSearchView } from './searchView.js';

/** 在实际创建处接线，使用户移动原搜索视图之后也使用同一业务 child。 */
export function createSearchView(instantiation: IInstantiationService, descriptor: IViewDescriptor, options: IViewletViewOptions) {
  const arguments_ = [...(descriptor.ctorDescriptor.staticArguments ?? []), options];
  if (descriptor.id !== VIEW_ID) return instantiation.createInstance(descriptor.ctorDescriptor.ctor, ...arguments_);
  const controller = new AbortController();
  // 恢复布局可以早于工作区初始化；首次查询才同步捕获目标，后续由同一视图固定范围。
  const load = async () => {
    const owner = instantiation.invokeFunction(accessor => captureSearchOwner(accessor.get(IEditorService)));
    return loadSearchScope([owner], controller.signal);
  };
  const context = new SearchWorkspaceContext();
  const child = context.ownChild(instantiation.createChild(new ServiceCollection([IWorkspaceContextService, context])));
  // 同一 transform 的原 VIEW_ID 描述符已选择 ScopedSearchView，并纳入生成源码类型检查。
  try {
    const view = child.createInstance(descriptor.ctorDescriptor.ctor, ...arguments_) as ScopedSearchView;
    view.initializeSearchScope(context, load, toDisposable(() => controller.abort()));
    return view;
  } catch (error) { controller.abort(); context.dispose(); throw error; }
}
