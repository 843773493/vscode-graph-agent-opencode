import { EditorInput } from '../../upstream/src/vs/workbench/common/editor/editorInput.js';
import type { IUntypedEditorInput } from '../../upstream/src/vs/workbench/common/editor.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { PersistentTaskDTO } from '../../../web/src/api/persistentTasks.js';
import { taskName } from '../../../web/src/components/workspace/persistentTasks/taskPresentation.js';

export type TaskPresentationAction = 'details' | 'history' | 'edit';
export class AutomationInput extends EditorInput {
  static readonly ID = 'boxteam.editor.automation';
  readonly resource: URI;
  constructor(readonly owner: WorkspaceOwner, readonly task: PersistentTaskDTO) {
    super();
    this.resource = URI.from({ scheme: 'boxteam-automation', authority: owner.gatewayId, path: `/${owner.workspaceId}/${task.task_id}` });
  }
  get typeId(): string { return AutomationInput.ID; }
  override get editorId(): string { return AutomationInput.ID; }
  override getName(): string { return taskName(this.task); }
  override getDescription(): string { return `${this.owner.gatewayId} · ${this.owner.workspaceId} · ${this.task.task_id}`; }
  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    return other instanceof AutomationInput && this.resource.toString() === other.resource.toString();
  }
}
