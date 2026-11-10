import { join } from 'node:path';

export const extensionTabsSourcePaths = ['src/vs/workbench/api/browser/mainThreadEditorTabs.ts'];

export function transformExtensionTabs(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', extensionTabsSourcePaths[0])) return source;
  let contents = replaceOnce(source, "import { ILogService } from '../../../platform/log/common/log.js';\n", '');
  contents = replaceOnce(contents, "import { isGroupEditorMoveEvent } from '../../common/editor/editorGroupModel.js';\n", '');
  contents = replaceOnce(contents, "import { URI } from '../../../base/common/uri.js';\n", '');
  contents = replaceOnce(contents, "import { EditorResourceAccessor, GroupModelChangeKind, SideBySideEditor } from '../../common/editor.js';", "import { GroupModelChangeKind } from '../../common/editor.js';");
  contents = replaceOnce(contents, '\t\t@ILogService private readonly _logService: ILogService,\n', '');
  contents = replaceOnce(contents, 'private readonly _proxy: IExtHostEditorTabsShape;', `private readonly _proxy: IExtHostEditorTabsShape;
  private _ready = false;
  private _disposed = false;
  private readonly _editorIds = new WeakMap<EditorInput, number>();
  private _nextEditorId = 0;`);
  contents = replaceOnce(contents, `\t\t\ttry {
\t\t\t\tthis._updateTabsModel(event);
\t\t\t} catch {
\t\t\t\tthis._logService.error('Failed to update model, rebuilding');
\t\t\t\tthis._createTabsModel();
\t\t\t}`, '\t\t\tthis._updateTabsModel(event);');
  contents = replaceOnce(contents, 'this._editorGroupsService.whenReady.then(() => this._createTabsModel());', `this._dispoables.add(this._editorGroupsService.onDidMoveGroup(() => this._createTabsModel()));
    this._editorGroupsService.whenReady.then(() => {
      if (this._disposed) return;
      this._ready = true;
      this._createTabsModel();
    });`);
  contents = replaceOnce(contents, '\tdispose(): void {', '\tdispose(): void {\n    this._disposed = true;\n    this._tabGroupModel = [];');
  const idStart = contents.indexOf('\n\tprivate _generateTabId(');
  const idEnd = contents.indexOf('\n\t/**\n\t * Called whenever a group activates', idStart);
  if (idStart < 0 || idEnd < 0) throw new Error('原 MainThreadEditorTabs 标签 ID 边界已变化');
  contents = replaceOnce(contents, contents.slice(idStart, idEnd), `
  private _generateTabId(editor: EditorInput, groupId: number): string {
    let id = this._editorIds.get(editor);
    if (id === undefined) this._editorIds.set(editor, id = ++this._nextEditorId);
    return groupId + '~' + id;
  }`);

  const start = contents.indexOf('\n\t/**\n\t * Called whenever a group activates');
  const end = contents.indexOf('\n\t//#region Messages received from Ext Host', start);
  if (start < 0 || end < 0) throw new Error('原 MainThreadEditorTabs 模型更新边界已变化');
  contents = replaceOnce(contents, contents.slice(start, end), `
  private _removeTab(groupId: number, editor: EditorInput): void {
    const id = this._generateTabId(editor, groupId), tabs = this._groupLookup.get(groupId)?.tabs;
    const index = tabs?.findIndex(tab => tab.id === id) ?? -1;
    if (!tabs || index < 0) return;
    const tabDto = tabs.splice(index, 1)[0];
    this._tabInfoLookup.delete(id);
    this._proxy.$acceptTabOperation({ groupId, index, tabDto, kind: TabModelOperationKind.TAB_CLOSE });
  }

  private _trackMultiDiff(group: IEditorGroup, editor: EditorInput): void {
    if (!(editor instanceof MultiDiffEditorInput)) return;
    const id = this._generateTabId(editor, group.id);
    if (this._multiDiffEditorInputListeners.has(id)) return;
    this._multiDiffEditorInputListeners.set(id, Event.fromObservableLight(editor.resources)(() => {
      const currentGroup = this._editorGroupsService.getGroup(group.id);
      if (!this._disposed && currentGroup?.editors.includes(editor)) this._refreshTab(currentGroup, editor);
    }));
  }

  private _refreshTab(group: IEditorGroup, editor: EditorInput, moved = false): void {
    this._trackMultiDiff(group, editor);
    const tabs = this._groupLookup.get(group.id)?.tabs;
    if (!tabs) throw new Error('标签事件先于所属编辑器组发布：' + group.id);
    const editorIndex = group.editors.indexOf(editor);
    if (editorIndex < 0) throw new Error('标签事件缺少原 EditorInput');
    const id = this._generateTabId(editor, group.id), oldIndex = tabs.findIndex(tab => tab.id === id);
    const updated = this._buildTabObject(group, editor, editorIndex);
    if (oldIndex < 0) {
      tabs.splice(editorIndex, 0, updated);
      this._tabInfoLookup.set(id, { group, editorInput: editor, tab: updated });
      this._proxy.$acceptTabOperation({ groupId: group.id, index: editorIndex, tabDto: updated, kind: TabModelOperationKind.TAB_OPEN });
    } else {
      const tab = tabs[oldIndex];
      Object.assign(tab, updated);
      if (moved && editorIndex !== oldIndex) {
        tabs.splice(oldIndex, 1); tabs.splice(editorIndex, 0, tab);
        this._proxy.$acceptTabOperation({ groupId: group.id, index: editorIndex, oldIndex, tabDto: tab, kind: TabModelOperationKind.TAB_MOVE });
      }
      this._proxy.$acceptTabOperation({ groupId: group.id, index: moved ? editorIndex : oldIndex, tabDto: tab, kind: TabModelOperationKind.TAB_UPDATE });
    }
  }

  private _syncActiveTabs(group: IEditorGroup): void {
    for (const [index, tab] of (this._groupLookup.get(group.id)?.tabs ?? []).entries()) {
      const info = this._tabInfoLookup.get(tab.id)!;
      const active = group.isActive(info.editorInput);
      if (tab.isActive !== active) {
        tab.isActive = active;
        this._proxy.$acceptTabOperation({ groupId: group.id, index, tabDto: tab, kind: TabModelOperationKind.TAB_UPDATE });
      }
    }
  }

  private _syncActiveGroups(): void {
    for (const group of this._tabGroupModel) {
      const active = group.groupId === this._editorGroupsService.activeGroup.id;
      if (group.isActive !== active) {
        group.isActive = active;
        this._proxy.$acceptTabGroupUpdate(group);
      }
    }
  }

  private _createTabsModel(): void {
    if (!this._ready || this._disposed || !this._editorGroupsService.groups.length) return;
    const previous = new Map(this._tabInfoLookup), multiDiffIds = new Set<string>();
    this._tabInfoLookup.clear();
    this._groupLookup.clear();
    this._tabGroupModel = this._editorGroupsService.groups.map(group => {
      const model: IEditorTabGroupDto = { groupId: group.id, isActive: group === this._editorGroupsService.activeGroup,
        viewColumn: editorGroupToColumn(this._editorGroupsService, group), tabs: [] };
      group.editors.forEach((editor, index) => {
        const id = this._generateTabId(editor, group.id);
        if (editor instanceof MultiDiffEditorInput) multiDiffIds.add(id);
        this._trackMultiDiff(group, editor);
        const updated = this._buildTabObject(group, editor, index);
        const tab = previous.get(id)?.tab ?? updated;
        if (tab !== updated) Object.assign(tab, updated);
        model.tabs.push(tab); this._tabInfoLookup.set(id, { group, editorInput: editor, tab });
      });
      this._groupLookup.set(group.id, model);
      return model;
    });
    for (const id of this._multiDiffEditorInputListeners.keys()) if (!multiDiffIds.has(id)) this._multiDiffEditorInputListeners.deleteAndDispose(id);
    this._proxy.$acceptEditorTabModel(this._tabGroupModel);
  }

  private _updateTabsModel({ groupId, event }: IEditorsChangeEvent): void {
    if (!this._ready || this._disposed) return;
    const group = this._editorGroupsService.getGroup(groupId);
    switch (event.kind) {
      case GroupModelChangeKind.GROUP_ACTIVE: this._syncActiveGroups(); return;
      case GroupModelChangeKind.GROUP_INDEX:
      case GroupModelChangeKind.GROUP_LABEL:
      case GroupModelChangeKind.GROUP_LOCKED:
      case GroupModelChangeKind.EDITORS_SELECTION:
      case GroupModelChangeKind.EDITOR_TRANSIENT:
      case GroupModelChangeKind.EDITOR_WILL_DISPOSE: return;
      case GroupModelChangeKind.EDITOR_CLOSE:
        if (!event.editor) throw new Error('标签关闭事件缺少原 EditorInput');
        this._multiDiffEditorInputListeners.deleteAndDispose(this._generateTabId(event.editor, groupId));
        this._removeTab(groupId, event.editor);
        if (group) this._syncActiveTabs(group);
        return;
      case GroupModelChangeKind.EDITOR_ACTIVE:
        if (group) this._syncActiveTabs(group);
        return;
      default:
        if (!group || !event.editor) throw new Error('标签变更事件缺少原编辑器组或 EditorInput');
        if (!this._groupLookup.has(groupId)) return;
        this._refreshTab(group, event.editor, event.kind === GroupModelChangeKind.EDITOR_MOVE);
    }
  }

  private _assertRunning(): void {
    if (this._disposed) throw new Error('标签同步已经释放');
  }

  private _isCurrentTab(info: TabInfo): boolean {
    return this._tabInfoLookup.get(info.tab.id)?.tab === info.tab
      && this._editorGroupsService.getGroup(info.group.id) === info.group
      && info.group.editors.includes(info.editorInput);
  }

  private _assertTab(tabId: string, info = this._tabInfoLookup.get(tabId)): TabInfo | undefined {
    this._assertRunning();
    if (info && !this._isCurrentTab(info)) throw new Error('标签请求已过期：' + tabId);
    return info;
  }
`);

  contents = replaceOnce(contents,
    '\t$moveTab(tabId: string, index: number, viewColumn: EditorGroupColumn, preserveFocus?: boolean): void {',
    '\t$moveTab(tabId: string, index: number, viewColumn: EditorGroupColumn, preserveFocus?: boolean): void {\n\t\tthis._assertRunning();');
  contents = replaceOnce(contents,
    '\t\tconst groupId = columnToEditorGroup(this._editorGroupsService, this._configurationService, viewColumn);\n\t\tconst tabInfo = this._tabInfoLookup.get(tabId);',
    '\t\tconst groupId = columnToEditorGroup(this._editorGroupsService, this._configurationService, viewColumn);\n\t\tconst tabInfo = this._assertTab(tabId);');
  const moveStart = contents.indexOf('\n\t\t// Similar logic to if index is out of bounds');
  const moveEnd = contents.indexOf('\n\t\t// Move the editor to the target group', moveStart);
  if (moveStart < 0 || moveEnd < 0) throw new Error('原标签移动索引边界已变化');
  contents = replaceOnce(contents, contents.slice(moveStart, moveEnd), `
    this._assertTab(tabId, tabInfo);
    const editorInput = tabInfo.editorInput;
    const sourceIndex = sourceGroup.editors.indexOf(editorInput);
    const available = targetGroup.editors.filter(editor => editor !== editorInput);
    const targetIndex = Math.max(0, Math.min(index, available.length));
    const anchor = available[targetIndex];
    index = anchor ? targetGroup.editors.indexOf(anchor) : targetGroup.editors.length;
    if (sourceGroup === targetGroup && sourceIndex < index) index--;
    if (sourceGroup === targetGroup && sourceIndex === index) return;
`);
  const closeStart = contents.indexOf('\n\tasync $closeTab('), closeEnd = contents.indexOf('\n\tasync $closeGroup(', closeStart);
  if (closeStart < 0 || closeEnd < 0) throw new Error('原标签关闭边界已变化');
  contents = replaceOnce(contents, contents.slice(closeStart, closeEnd), `
  async $closeTab(tabIds: string[], preserveFocus?: boolean): Promise<boolean> {
    this._assertRunning();
    const groups: Map<IEditorGroup, TabInfo[]> = new Map();
    for (const tabId of tabIds) {
      const info = this._assertTab(tabId);
      if (!info) continue;
      const infos = groups.get(info.group);
      if (infos) infos.push(info); else groups.set(info.group, [info]);
    }
    const results: boolean[] = [];
    for (const [group, infos] of groups) {
      this._assertRunning();
      if (infos.some(info => !this._isCurrentTab(info))) throw new Error('标签请求已过期；前序关闭结果=' + JSON.stringify(results) + '，未执行后继关闭');
      results.push(await group.closeEditors(infos.map(info => info.editorInput), { preserveFocus }));
      if (this._disposed && results.length < groups.size) throw new Error('标签同步已经释放；前序关闭结果=' + JSON.stringify(results) + '，未执行后继关闭');
    }
    return results.every(result => result);
  }
`);
  const groupCloseStart = contents.indexOf('\n\tasync $closeGroup(');
  const groupCloseEnd = contents.indexOf('\n\t//#endregion', groupCloseStart);
  if (groupCloseStart < 0 || groupCloseEnd < 0) throw new Error('原标签组关闭边界已变化');
  contents = replaceOnce(contents, contents.slice(groupCloseStart, groupCloseEnd), `
  async $closeGroup(groupIds: number[], preserveFocus?: boolean): Promise<boolean> {
    this._assertRunning();
    const groups = groupIds.map(id => this._editorGroupsService.getGroup(id));
    const results: boolean[] = [];
    for (const [index, group] of groups.entries()) {
      this._assertRunning();
      if (!group) continue;
      if (this._editorGroupsService.getGroup(group.id) !== group) throw new Error('编辑器组请求已过期；前序关闭结果=' + JSON.stringify(results) + '，未执行后继关闭');
      results.push(await group.closeAllEditors());
      if (this._disposed) {
        if (groups.slice(index + 1).some(group => group !== undefined)) throw new Error('标签同步已经释放；前序关闭结果=' + JSON.stringify(results) + '，未执行后继关闭');
        return results.every(result => result);
      }
      if (group.count === 0 && this._editorGroupsService.getGroup(group.id) === group) this._editorGroupsService.removeGroup(group);
    }
    return results.every(result => result);
  }
`);
  return contents;
}
