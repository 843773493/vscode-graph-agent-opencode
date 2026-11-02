import { Action, Separator } from '../upstream/src/vs/base/common/actions.js';
import type { IContextMenuService } from '../upstream/src/vs/platform/contextview/browser/contextView.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorService, SIDE_GROUP } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { GroupDirection, IEditorGroup, IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { OpenWithEditorPicker } from '../upstream/src/vs/workbench/services/editor/browser/openWithEditorPicker.js';
import { resourceFileRootUri } from '../../web/src/utils/workspaceFileReferences.js';
import type { ThreadOwner } from './agentApi.js';
import { workspaceFileUri } from './workspaceFiles.js';

/** 原生菜单及打开方式共用同一后端文件 URI，所有动作冻结右键时的工作区归属。 */
export function showFileOpenMenu(path: string, anchor: { x: number; y: number }, owner: ThreadOwner,
  menus: IContextMenuService, editors: IEditorService, groups: IEditorGroupsService,
  instantiation: IInstantiationService, group: IEditorGroup): void {
  const root = resourceFileRootUri(path);
  if (!root) throw new Error('文件缺少后端资源根 URI');
  const resource = workspaceFileUri(owner, path, root);
  const actions = [
    new Action('boxteam.file.open', '在编辑器中打开', '', true,
      () => editors.openEditor({ resource, options: { pinned: true } }, group.id)),
    new Action('boxteam.file.preview', '在编辑器中预览', '', true,
      () => editors.openEditor({ resource, options: { pinned: false } }, group.id)),
    new Action('boxteam.file.openSide', '在侧边打开', '', true,
      () => editors.openEditor({ resource, options: { pinned: true } }, SIDE_GROUP)),
    new Action('boxteam.file.openBelow', '在下方打开', '', true, () => {
      const target = groups.findGroup({ direction: GroupDirection.DOWN }, group) ?? groups.addGroup(group, GroupDirection.DOWN);
      return editors.openEditor({ resource, options: { pinned: true } }, target.id);
    }),
    new Separator(),
    new Action('boxteam.file.openWith', '打开方式…', '', true, async () => {
      const options = await instantiation.createInstance(OpenWithEditorPicker).pick({ resource });
      if (options) await editors.openEditor({ resource, options: { ...options, pinned: true } }, group.id);
    }),
  ];
  menus.showContextMenu({ getAnchor: () => anchor, getActions: () => actions,
    onHide: () => actions.forEach(action => { if (action instanceof Action) action.dispose(); }) });
}
