import { t } from '../../shared/core/i18n/index.js';
import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { SyncDescriptor } from '../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { ITelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IThemeService } from '../upstream/src/vs/platform/theme/common/themeService.js';
import { IStorageService } from '../upstream/src/vs/platform/storage/common/storage.js';
import { EditorPaneDescriptor, type IEditorPaneRegistry } from '../upstream/src/vs/workbench/browser/editor.js';
import { EditorPlaceholder, type IEditorPlaceholderContents } from '../upstream/src/vs/workbench/browser/parts/editor/editorPlaceholder.js';
import { EditorExtensions } from '../upstream/src/vs/workbench/common/editor.js';
import type { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import type { IEditorGroup } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { UnavailableReadonlyDocumentInput } from './readonlyDocuments.js';

/** 复用原占位界面和组关闭操作，坏记录不消失，也不阻塞同组其它资源恢复。 */
class UnavailableReadonlyDocumentPane extends EditorPlaceholder {
  static readonly ID = 'boxteam.editor.unavailableReadonlyDocument';
  constructor(group: IEditorGroup, @ITelemetryService telemetry: ITelemetryService,
    @IThemeService themes: IThemeService, @IStorageService storage: IStorageService) {
    super(UnavailableReadonlyDocumentPane.ID, group, telemetry, themes, storage);
  }
  protected override async getContents(input: EditorInput): Promise<IEditorPlaceholderContents> {
    if (!(input instanceof UnavailableReadonlyDocumentInput)) throw new Error(t('workbench.readonly.placeholderInputRequired'));
    return { icon: '$(error)', label: input.reason,
      actions: [{ label: t('workbench.readonly.removeRecord'), run: () => this.group.closeEditor(input) }] };
  }
}

Registry.as<IEditorPaneRegistry>(EditorExtensions.EditorPane).registerEditorPane(
  EditorPaneDescriptor.create(UnavailableReadonlyDocumentPane, UnavailableReadonlyDocumentPane.ID, t('workbench.readonly.unavailableTitle')),
  [new SyncDescriptor(UnavailableReadonlyDocumentInput)],
);
