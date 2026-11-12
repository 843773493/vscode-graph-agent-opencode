import { Action, Separator } from '../upstream/src/vs/base/common/actions.js';
import { t } from '../../shared/core/i18n/index.js';
import type { IContextMenuService } from '../upstream/src/vs/platform/contextview/browser/contextView.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorService, SIDE_GROUP } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { GroupDirection, IEditorGroup, IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { OpenWithEditorPicker } from '../upstream/src/vs/workbench/services/editor/browser/openWithEditorPicker.js';
import { resourceFileRootUri } from '../../web/src/utils/workspaceFileReferences.js';
import type { ThreadOwner } from './agentApi.js';
import { workspaceFileUri } from './workspaceFiles.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { ATTACH_RESOURCE_URI, COPY_RESOURCE_URI } from './resourceActions.js';

/** 原生菜单及打开方式共用同一后端文件 URI，所有动作冻结右键时的工作区归属。 */
export function showFileOpenMenu(path: string, anchor: { x: number; y: number }, owner: ThreadOwner,
  menus: IContextMenuService, editors: IEditorService, groups: IEditorGroupsService,
  instantiation: IInstantiationService, group: IEditorGroup): void {
  const root = resourceFileRootUri(path);
  if (!root) throw new Error(t('workbench.fileMenu.rootMissing'));
  const resource = workspaceFileUri(owner, path, root);
  const actions = [
    new Action('boxteam.file.open', t('workbench.fileMenu.open'), '', true,
      () => editors.openEditor({ resource, options: { pinned: true } }, group.id)),
    new Action('boxteam.file.preview', t('workbench.fileMenu.preview'), '', true,
      () => editors.openEditor({ resource, options: { pinned: false } }, group.id)),
    new Action('boxteam.file.openSide', t('workbench.fileMenu.openSide'), '', true,
      () => editors.openEditor({ resource, options: { pinned: true } }, SIDE_GROUP)),
    new Action('boxteam.file.openBelow', t('workbench.fileMenu.openBelow'), '', true, () => {
      const target = groups.findGroup({ direction: GroupDirection.DOWN }, group) ?? groups.addGroup(group, GroupDirection.DOWN);
      return editors.openEditor({ resource, options: { pinned: true } }, target.id);
    }),
    new Separator(),
    new Action(COPY_RESOURCE_URI, t('workbench.resource.copyUri'), '', true,
      () => instantiation.invokeFunction(accessor => accessor.get(ICommandService).executeCommand(COPY_RESOURCE_URI, resource))),
    new Action(ATTACH_RESOURCE_URI, t('workbench.resource.attachToChat'), '', true,
      () => instantiation.invokeFunction(accessor => accessor.get(ICommandService).executeCommand(ATTACH_RESOURCE_URI, resource))),
    new Action('boxteam.file.openWith', t('workbench.fileMenu.openWith'), '', true, async () => {
      const options = await instantiation.createInstance(OpenWithEditorPicker).pick({ resource });
      if (options) await editors.openEditor({ resource, options: { ...options, pinned: true } }, group.id);
    }),
  ];
  menus.showContextMenu({ getAnchor: () => anchor, getActions: () => actions,
    onHide: () => actions.forEach(action => { if (action instanceof Action) action.dispose(); }) });
}
