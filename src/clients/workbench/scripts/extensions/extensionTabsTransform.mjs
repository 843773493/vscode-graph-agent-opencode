import { join } from 'node:path';

export const extensionTabsSourcePaths = ['src/vs/workbench/api/browser/mainThreadEditorTabs.ts'];

/** 标签仍由原 MainThread owner 持有；组内索引属于该宿主实际可见的同一数组。 */
export function transformExtensionTabs(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', extensionTabsSourcePaths[0])) return source;
  let contents = `import { hostOwnsWorkspaceResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};\n` + source;
  contents = replaceOnce(contents, "import { ILogService } from '../../../platform/log/common/log.js';\n", '');
  contents = replaceOnce(contents, "import { isGroupEditorMoveEvent } from '../../common/editor/editorGroupModel.js';\n", '');
  contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
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
  contents = replaceOnce(contents, 'private _generateTabId(editor: EditorInput, groupId: number) {', `private _generateTabId(editor: EditorInput, groupId: number) {
    if (this.extHostContext.extensionHost.targetContext) {
      // 目标标签 ID 只标识原 EditorInput，不夹带 MultiDiff source 或本机路径。
      let id = this._editorIds.get(editor);
      if (id === undefined) this._editorIds.set(editor, id = ++this._nextEditorId);
      return groupId + '~' + id;
    }`);
  const start = contents.indexOf('\n\t/**\n\t * Called whenever a group activates');
  const end = contents.indexOf('\n\t//#region Messages received from Ext Host', start);
  if (start < 0 || end < 0) throw new Error('原 MainThreadEditorTabs 模型更新边界已变化');
  contents = replaceOnce(contents, contents.slice(start, end), `
  private _acceptsEditor(editor: EditorInput): boolean {
    if (!this.extHostContext.extensionHost.targetContext) return true;
    const owns = (uri: URI | undefined) => !!uri && hostOwnsWorkspaceResource(this.extHostContext.extensionHost, uri);
    if (editor instanceof MultiDiffEditorInput) {
      const resources = editor.resources.get() ?? [];
      // 整个 EditorInput 是关闭／移动单位；归属检查包含原 DTO 未编码的单边项与跳转位置。
      return resources.length > 0 && resources.every(resource => {
        const endpoints = [resource.originalUri, resource.modifiedUri, resource.goToFileUri].filter((uri): uri is URI => !!uri);
        return endpoints.length > 0 && endpoints.every(owns);
      });
    }
    if (editor instanceof MergeEditorInput) return [editor.base, editor.input1.uri, editor.input2.uri, editor.resource].every(owns);
    if (editor instanceof SideBySideEditorInput) return this._acceptsEditor(editor.primary) && this._acceptsEditor(editor.secondary);
    if (editor instanceof InteractiveEditorInput) return owns(editor.resource) && owns(editor.inputResource);
    // TODO: terminal／webview／chat 等无工作区文件的标签，待各 API owner 提供目标归属后接入。
    return owns(editor.resource);
  }

  private _projectedIndex(group: IEditorGroup, editor: EditorInput): number {
    return group.editors.filter(candidate => this._acceptsEditor(candidate)).indexOf(editor);
  }

  private _trackMultiDiff(group: IEditorGroup, editor: EditorInput): void {
    if (!(editor instanceof MultiDiffEditorInput)) return;
    const id = this._generateTabId(editor, group.id);
    if (this._multiDiffEditorInputListeners.has(id)) return;
    this._multiDiffEditorInputListeners.set(id, Event.fromObservableLight(editor.resources)(() => {
      if (!this._disposed && this._editorGroupsService.getGroup(group.id) === group && group.editors.includes(editor)) {
        this._refreshTab(group, editor);
      }
    }));
  }

  private _removeTab(groupId: number, editor: EditorInput): void {
    const id = this._generateTabId(editor, groupId), tabs = this._groupLookup.get(groupId)?.tabs;
    const index = tabs?.findIndex(tab => tab.id === id) ?? -1;
    if (!tabs || index < 0) return;
    const tabDto = tabs.splice(index, 1)[0];
    this._tabInfoLookup.delete(id);
    this._proxy.$acceptTabOperation({ groupId, index, tabDto, kind: TabModelOperationKind.TAB_CLOSE });
  }

  private _refreshTab(group: IEditorGroup, editor: EditorInput, moved = false): void {
    this._trackMultiDiff(group, editor);
    const tabs = this._groupLookup.get(group.id)?.tabs;
    if (!tabs) throw new Error('标签事件先于所属编辑器组发布：' + group.id);
    if (!this._acceptsEditor(editor)) { this._removeTab(group.id, editor); this._syncActiveTabs(group); return; }
    const editorIndex = group.editors.indexOf(editor);
    if (editorIndex < 0) throw new Error('标签事件缺少原 EditorInput');
    const id = this._generateTabId(editor, group.id), oldIndex = tabs.findIndex(tab => tab.id === id);
    const updated = this._buildTabObject(group, editor, editorIndex);
    if (oldIndex < 0) {
      const index = this._projectedIndex(group, editor);
      tabs.splice(index, 0, updated);
      this._tabInfoLookup.set(id, { group, editorInput: editor, tab: updated });
      this._proxy.$acceptTabOperation({ groupId: group.id, index, tabDto: updated, kind: TabModelOperationKind.TAB_OPEN });
    } else {
      const tab = tabs[oldIndex];
      Object.assign(tab, updated);
      const index = moved ? this._projectedIndex(group, editor) : oldIndex;
      if (moved && index !== oldIndex) {
        tabs.splice(oldIndex, 1); tabs.splice(index, 0, tab);
        this._proxy.$acceptTabOperation({ groupId: group.id, index, oldIndex, tabDto: tab, kind: TabModelOperationKind.TAB_MOVE });
      }
      this._proxy.$acceptTabOperation({ groupId: group.id, index, tabDto: tab, kind: TabModelOperationKind.TAB_UPDATE });
    }
    this._syncActiveTabs(group);
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
        if (!this._acceptsEditor(editor)) return;
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
    for (const group of this._editorGroupsService.groups) for (const editor of group.editors) this._trackMultiDiff(group, editor);
  }

  private _updateTabsModel({ groupId, event }: IEditorsChangeEvent): void {
    if (!this._ready || this._disposed) return;
    const group = this._editorGroupsService.getGroup(groupId);
    switch (event.kind) {
      case GroupModelChangeKind.GROUP_ACTIVE: this._syncActiveGroups(); return;
      // add／remove／move 的布局 owner 已发布结构；逐组 index 通知不再重复整窗同步。
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
        // modal editor 没有进入原 groups 模型，不向普通 TabGroups 发布。
        if (!this._groupLookup.has(groupId)) return;
        this._refreshTab(group, event.editor, event.kind === GroupModelChangeKind.EDITOR_MOVE);
    }
  }

  private _assertRunning(): void {
    if (this._disposed) throw new Error('标签所属宿主代际已经关闭');
  }

  private _assertTab(tabId: string): TabInfo | undefined {
    this._assertRunning();
    const info = this._tabInfoLookup.get(tabId);
    if (this.extHostContext.extensionHost.targetContext && (!info || !info.group.editors.includes(info.editorInput) || !this._acceptsEditor(info.editorInput))) {
      throw new Error('标签不属于发起宿主的当前工作区视图：' + tabId);
    }
    return info;
  }
`);
  contents = replaceOnce(contents, '\t\tconst groupId = columnToEditorGroup(this._editorGroupsService, this._configurationService, viewColumn);\n\t\tconst tabInfo = this._tabInfoLookup.get(tabId);', '\t\tconst tabInfo = this._assertTab(tabId);');
  contents = replaceOnce(contents, '\t\tlet targetGroup: IEditorGroup | undefined;', '\t\tconst groupId = columnToEditorGroup(this._editorGroupsService, this._configurationService, viewColumn);\n\t\tlet targetGroup: IEditorGroup | undefined;');
  const moveStart = contents.indexOf('\n\t\t// Similar logic to if index is out of bounds');
  const moveEnd = contents.indexOf('\n\t\t// Move the editor to the target group', moveStart);
  if (moveStart < 0 || moveEnd < 0) throw new Error('原标签移动索引边界已变化');
  contents = replaceOnce(contents, contents.slice(moveStart, moveEnd), `
    this._assertTab(tabId);
    const editorInput = tabInfo.editorInput;
    const visible = targetGroup.editors.filter(editor => editor !== editorInput && this._acceptsEditor(editor));
    if (sourceGroup === targetGroup && this._projectedIndex(sourceGroup, editorInput) === (index < 0 || index > visible.length ? visible.length : index)) return;
    const anchor = visible[index < 0 || index > visible.length ? visible.length : index];
    index = anchor ? targetGroup.editors.indexOf(anchor) : targetGroup.editors.length;
    if (sourceGroup === targetGroup && sourceGroup.editors.indexOf(editorInput) < index) index--;
`);
  contents = replaceOnce(contents, 'const groups: Map<IEditorGroup, EditorInput[]> = new Map();', `this._assertRunning();
    const selected = tabIds.map(id => this._assertTab(id));
    const groups: Map<IEditorGroup, EditorInput[]> = new Map();`);
  contents = replaceOnce(contents, 'for (const tabId of tabIds) {\n\t\t\tconst tabInfo = this._tabInfoLookup.get(tabId);', 'for (const tabInfo of selected) {');
  contents = replaceOnce(contents, 'results.push(await group.closeEditors(editors, { preserveFocus }));', `this._assertRunning();
      if (editors.some(editor => !this._acceptsEditor(editor))) throw new Error('待关闭标签的资源归属已经变化');
      results.push(await group.closeEditors(editors, { preserveFocus }));
      if (this._disposed && results.length < groups.size) throw new Error('标签宿主代际已经关闭；前序关闭结果=' + JSON.stringify(results) + '，未执行后继关闭');`);
  contents = replaceOnce(contents, 'const groupCloseResults: boolean[] = [];', `this._assertRunning();
    if (this.extHostContext.extensionHost.targetContext && groupIds.some(id => !this._groupLookup.has(id))) throw new Error('编辑器组不属于发起宿主的当前标签模型');
    const groupCloseResults: boolean[] = [];`);
  contents = replaceOnce(contents, 'groupCloseResults.push(await group.closeAllEditors());', `this._assertRunning();
        const editors = group.editors.filter(editor => this._tabInfoLookup.has(this._generateTabId(editor, group.id)) && this._acceptsEditor(editor));
        groupCloseResults.push(await (this.extHostContext.extensionHost.targetContext
          ? group.closeEditors(editors, { preserveFocus }) : group.closeAllEditors()));
        if (this._disposed) {
          if (index + 1 < groupIds.length) throw new Error('标签宿主代际已经关闭；前序关闭结果=' + JSON.stringify(groupCloseResults) + '，未执行后继关闭');
          return groupCloseResults.every(result => result);
        }`);
  contents = replaceOnce(contents, 'for (const groupId of groupIds) {', 'for (const [index, groupId] of groupIds.entries()) {');
  return contents;
}
