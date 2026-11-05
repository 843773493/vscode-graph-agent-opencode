import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { SyncDescriptor } from '../../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { EditorPaneDescriptor, IEditorPaneRegistry } from '../../upstream/src/vs/workbench/browser/editor.js';
import { EditorExtensions } from '../../upstream/src/vs/workbench/common/editor.js';
import { AutomationInput } from './input.js';
import { AutomationPane } from './pane.js';
import './actions.js';
import './quickAccess.js';

Registry.as<IEditorPaneRegistry>(EditorExtensions.EditorPane).registerEditorPane(
  EditorPaneDescriptor.create(AutomationPane, AutomationPane.ID, '自动化任务'), [new SyncDescriptor(AutomationInput)],
);
