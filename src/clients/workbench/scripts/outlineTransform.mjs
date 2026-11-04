import { join } from 'node:path';

export const outlineSourcePaths = [
  'src/vs/workbench/services/outline/browser/outline.ts',
  'src/vs/workbench/contrib/codeEditor/browser/quickaccess/gotoSymbolQuickAccess.ts',
];

/** 原非文本 Provider 消费分页大纲更新，继续使用原过滤、预览和取消机制。 */
export function transformOutline(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', outlineSourcePaths[0])) {
    return replaceOnce(source, 'export interface IQuickPickOutlineElement<E> {',
      'export interface IQuickPickOutlineElement<E> {\n\treadonly acceptInPlace?: boolean;');
  }
  if (path !== join(root, 'upstream', outlineSourcePaths[1])) return source;
  let contents = replaceOnce(source, 'interface INavigablePickEntry {',
    'interface INavigablePickEntry {\n\treadonly identity: unknown;\n\treadonly acceptInPlace?: boolean;');
  contents = replaceOnce(contents, 'disposables, this.outlineToNavigableEntries(outline));\n\t\treturn disposables;',
    'disposables, () => this.outlineToNavigableEntries(outline), outline.onDidChange);\n\t\treturn disposables;');
  contents = replaceOnce(contents, 'disposables, this.outlineToNavigableEntries(outline));\n\n\t\t}).catch',
    'disposables, () => this.outlineToNavigableEntries(outline), outline.onDidChange);\n\n\t\t}).catch');
  contents = replaceOnce(contents, 'label: element.label,\n\t\t\tdescription: element.description,',
    'identity: element.element,\n\t\t\tacceptInPlace: element.acceptInPlace,\n\t\t\tlabel: element.label,\n\t\t\tdescription: element.description,');
  contents = replaceOnce(contents, 'entries: readonly INavigablePickEntry[]): void {\n\t\tconst items: IGotoSymbolQuickPickItem[] = entries.map',
    'getEntries: () => readonly INavigablePickEntry[], onDidChange: Event<unknown>): void {\n\t\tlet updating = false;\n\t\tlet entries = getEntries();\n\t\tconst toItems = () => entries.map');
  contents = replaceOnce(contents, '\n\t\tdisposables.add(picker.onDidAccept(() => {\n\t\t\tpicker.hide();\n\t\t\tconst [item] = picker.selectedItems;\n\t\t\tif (item) {\n\t\t\t\tentries[item.index]?.reveal();\n\t\t\t}\n\t\t}));', `
    let items: IGotoSymbolQuickPickItem[] = toItems();
    disposables.add(picker.onDidAccept(() => {
      const [item] = picker.selectedItems;
      const entry = item ? entries[item.index] : undefined;
      if (!entry?.acceptInPlace) picker.hide();
      else picker.selectedItems = [];
      entry?.reveal();
    }));`);
  contents = replaceOnce(contents, '\t\tupdatePickerItems();\n\t\tdisposables.add(picker.onDidChangeValue(updatePickerItems));', `
    updatePickerItems();
    disposables.add(picker.onDidChangeValue(updatePickerItems));
    disposables.add(onDidChange(() => {
      updating = true;
      const active = picker.activeItems[0];
      const identity = active ? entries[active.index]?.identity : undefined;
      entries = getEntries();
      items = toItems();
      const keepScrollPosition = picker.keepScrollPosition;
      picker.keepScrollPosition = true;
      updatePickerItems();
      const retained = items.find(item => entries[item.index].identity === identity);
      if (retained && picker.items.includes(retained)) picker.activeItems = [retained];
      else if (active) { picker.activeItems = []; previewDisposable.clear(); }
      picker.keepScrollPosition = keepScrollPosition;
      updating = false;
    }));`);
  contents = replaceOnce(contents, "if (picker.value === '@') {", "if (picker.value === '@' || entries[item.index].acceptInPlace) {");
  contents = replaceOnce(contents, 'disposables.add(picker.onDidChangeActive(() => {\n\t\t\tconst [item] = picker.activeItems;',
    'disposables.add(picker.onDidChangeActive(() => {\n\t\t\tif (updating) return;\n\t\t\tconst [item] = picker.activeItems;');
  return contents;
}
