import { EditorPane } from '../../upstream/src/vs/workbench/browser/parts/editor/editorPane.js';
import { EditorInput } from '../../upstream/src/vs/workbench/common/editor/editorInput.js';
import type { IEditorOpenContext } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorOptions } from '../../upstream/src/vs/platform/editor/common/editor.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroup } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IThemeService } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { IStorageService } from '../../upstream/src/vs/platform/storage/common/storage.js';
import type { Dimension } from '../../upstream/src/vs/base/browser/dom.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { gatewayWorkspaceKey } from '../../../web/src/api/workspaceIdentity.js';
import type { PersistentTaskDTO } from '../../../web/src/api/persistentTasks.js';
import { gateway } from '../agentApi.js';
import { ReactSurface } from '../react/surface.js';
import { TaskView } from '../react/taskView.js';
import { productStyles } from '../react/productStyles.js';
import { AutomationInput, type TaskPresentationAction } from './input.js';

export interface AutomationEditorOptions extends IEditorOptions { automationAction?: TaskPresentationAction; automationTask?: PersistentTaskDTO }

/** 只托管原任务产品；底部面板继续属于导航中的当前工作区。 */
export class AutomationPane extends EditorPane {
  static readonly ID = 'boxteam.editor.automationPane';
  private container!: HTMLElement;
  private surface?: ReactSurface;
  private current?: AutomationInput;
  private sequence = 0;
  private action: TaskPresentationAction = 'details';
  private task?: PersistentTaskDTO;
  constructor(group: IEditorGroup,
    @ITelemetryService telemetry: ITelemetryService, @IThemeService themes: IThemeService,
    @IStorageService storage: IStorageService, @IEditorService private readonly editors: IEditorService,
  ) { super(AutomationPane.ID, group, telemetry, themes, storage); }
  protected override createEditor(parent: HTMLElement): void {
    this.container = document.createElement('section');
    this.container.style.cssText = 'height:100%;overflow:auto';
    parent.append(this.container);
  }
  override async setInput(input: EditorInput, options: AutomationEditorOptions | undefined, context: IEditorOpenContext, token: CancellationToken): Promise<void> {
    this.clearInput();
    await super.setInput(input, options, context, token);
    if (!(input instanceof AutomationInput)) throw new Error('自动化编辑器需要明确任务 owner');
    await gateway.resolveWorkspace(input.owner);
    if (token.isCancellationRequested || this.input !== input) return;
    this.current = input;
    this.surface = new ReactSurface(this.container, productStyles);
    this.setOptions(options);
  }
  override setOptions(options: AutomationEditorOptions | undefined): void {
    super.setOptions(options);
    this.action = options?.automationAction ?? 'details';
    this.task = options?.automationTask ?? this.current?.task;
    ++this.sequence; this.render();
  }
  protected override setEditorVisible(visible: boolean): void { super.setEditorVisible(visible); this.render(); }
  private render(): void {
    if (!this.current) return;
    const { owner } = this.current;
    const task = this.task ?? this.current.task;
    this.surface?.render(<TaskView owner={{ workspaceKey: gatewayWorkspaceKey(owner), workspaceId: owner.workspaceId }}
      editors={this.editors} active={this.isVisible()} intent={{ task, action: this.action, sequence: this.sequence }} />);
  }
  override clearInput(): void { this.surface?.dispose(); this.surface = undefined; this.current = undefined; this.task = undefined; super.clearInput(); }
  override layout(dimension: Dimension): void { this.container.style.width = `${dimension.width}px`; this.container.style.height = `${dimension.height}px`; }
  override focus(): void { this.surface?.focus('.persistent-task-detail :is(button,input)'); }
  override dispose(): void { this.clearInput(); super.dispose(); }
}
