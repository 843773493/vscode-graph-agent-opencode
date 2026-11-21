import { CodeEditorWidget } from '../../upstream/src/vs/editor/browser/widget/codeEditor/codeEditorWidget.js';
import { IModelService } from '../../upstream/src/vs/editor/common/services/model.js';
import { ILanguageService } from '../../upstream/src/vs/editor/common/languages/language.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import type { IEditorOptions } from '../../upstream/src/vs/editor/common/config/editorOptions.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ScrollType } from '../../upstream/src/vs/editor/common/editorCommon.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { WorkspaceCodePreviewRenderer } from '../../../web/src/components/workspace/preview/WorkspaceCodePreview.js';

export function createCodePreviewRenderer(instantiation: IInstantiationService): WorkspaceCodePreviewRenderer {
  return (container, initial) => instantiation.invokeFunction(accessor => {
    const models = accessor.get(IModelService);
    const languages = accessor.get(ILanguageService);
    const configuration = accessor.get(IConfigurationService);
    const disposables = new DisposableStore();
    let props = initial;
    // 投影至宿主 light DOM，直接使用原 Monaco 样式、主题和浮层，避免复制 CSS 到 ShadowRoot。
    const root = container.getRootNode();
    if (!(root instanceof ShadowRoot)) throw new Error('文件预览必须挂载于 Workbench React 宿主');
    const slot = document.createElement('slot');
    slot.name = `code-preview-${generateUuid()}`;
    slot.style.cssText = 'display:block;width:100%;height:100%;min-width:0;min-height:0';
    const element = document.createElement('div');
    element.slot = slot.name;
    element.style.cssText = 'width:100%;height:100%;overflow:hidden';
    container.append(slot);
    root.host.append(element);
    const resource = URI.from({ scheme: 'boxteam-preview', path: `/${generateUuid()}/${initial.path.split('/').at(-1)}` });
    const model = models.createModel(initial.content, languages.isRegisteredLanguageId(initial.language)
      ? languages.createById(initial.language) : languages.createByFilepathOrFirstLine(resource, initial.content.split('\n', 1)[0]), resource);
    const options = () => ({ ...configuration.getValue<IEditorOptions>('editor'), readOnly: props.readOnly,
      minimap: { enabled: false }, automaticLayout: false, scrollBeyondLastLine: false });
    const editor = instantiation.createInstance(CodeEditorWidget, element, options(), {});
    editor.setModel(model);
    disposables.add(editor.onDidChangeModelContent(() => {
      if (!props.readOnly) props.onChange(model.getValue());
    }));
    disposables.add(editor.onMouseDown(() => { if (props.readOnly) props.onBeginEdit(); }));
    disposables.add(editor.onKeyDown(event => {
      if ((event.ctrlKey || event.metaKey) && event.browserEvent.key.toLowerCase() === 's') {
        event.preventDefault(); event.stopPropagation(); props.onSave();
      }
    }));
    disposables.add(configuration.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration('editor')) editor.updateOptions(options());
    }));
    const layout = () => editor.layout({ width: container.clientWidth, height: container.clientHeight });
    const observer = new ResizeObserver(layout);
    observer.observe(container);
    layout();
    let selected = initial.selection;
    const reveal = () => {
      if (!props.selection) return;
      const { startLine, endLine, startColumn } = props.selection;
      const range = model.validateRange({ startLineNumber: startLine, startColumn: startColumn ?? 1,
        endLineNumber: endLine, endColumn: startColumn !== undefined && endLine === startLine
          ? startColumn : model.getLineMaxColumn(Math.min(Math.max(1, endLine), model.getLineCount())) });
      editor.setSelection(range);
      editor.revealRangeInCenterIfOutsideViewport(range, ScrollType.Immediate);
    };
    reveal();
    return {
      update(next) {
        const focus = props.readOnly && !next.readOnly;
        const modeChanged = props.readOnly !== next.readOnly;
        const contentChanged = props.content !== next.content;
        props = next;
        if (contentChanged && model.getValue() !== props.content) model.setValue(props.content);
        if (modeChanged) editor.updateOptions(options());
        if (selected !== props.selection) { selected = props.selection; reveal(); }
        if (focus) editor.focus();
      },
      dispose() {
        observer.disconnect(); disposables.dispose(); editor.dispose(); model.dispose();
        element.remove(); slot.remove();
      },
    };
  });
}
