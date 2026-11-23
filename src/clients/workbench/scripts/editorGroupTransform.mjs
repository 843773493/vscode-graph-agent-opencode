import { join } from 'node:path';

export const editorGroupSourcePaths = [
  'src/vs/platform/editor/common/editor.ts',
  'src/vs/workbench/services/editor/common/editorGroupFinder.ts',
];

/** 发起组只是原选组算法的起点；锁定、复用、焦点和明确目标仍沿原生规则。 */
export function transformEditorGroups(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', editorGroupSourcePaths[0])) {
    return replaceOnce(source, 'export interface IEditorOptions {', `export interface IEditorOptions {
  /** 普通打开和侧开的发起组，不是强制目标；不传时沿原活动组规则。 */
  sourceGroup?: number;
  /** 单次侧开的方向；未指定时使用原工作台设置。 */
  sideBySideDirection?: 'right' | 'down';`);
  }
  if (path === join(root, 'upstream', editorGroupSourcePaths[1])) {
    let contents = "import { localize } from '../../../../nls.js';\n" + source;
    contents = replaceOnce(contents, 'IEditorGroup, GroupsOrder,', 'IEditorGroup, GroupDirection, GroupsOrder,');
    contents = replaceOnce(contents, 'const options = input.options;', `const options = input.options;
  const sourceGroup = options?.sourceGroup === undefined ? editorGroupService.activeGroup : editorGroupService.getGroup(options.sourceGroup);
  if (!sourceGroup) throw new Error(localize('editorSourceGroupClosed', "The editor group that initiated this action has been closed."));`);
    contents = replaceOnce(contents, 'const direction = preferredSideBySideGroupDirection(configurationService);',
      "const direction = options?.sideBySideDirection === undefined ? preferredSideBySideGroupDirection(configurationService) : options.sideBySideDirection === 'down' ? GroupDirection.DOWN : GroupDirection.RIGHT;");
    contents = replaceOnce(contents, 'editorGroupService.findGroup({ direction })', 'editorGroupService.findGroup({ direction }, sourceGroup)');
    contents = replaceOnce(contents, 'editorGroupService.addGroup(editorGroupService.activeGroup, direction)', 'editorGroupService.addGroup(sourceGroup, direction)');
    return replaceOnce(contents, 'let candidateGroup = editorGroupService.activeGroup;', 'let candidateGroup = sourceGroup;');
  }
  return source;
}
