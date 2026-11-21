import { Disposable, MutableDisposable, toDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { truncate } from '../upstream/src/vs/base/common/strings.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IStatusbarService, StatusbarAlignment, type IStatusbarEntryAccessor } from '../upstream/src/vs/workbench/services/statusbar/browser/statusbar.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { t } from '../../shared/core/i18n/index.js';
import { workspaceContext } from './workspaceContext.js';
import { captureSearchOwner } from './search/target.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import { ThreadInput } from './threadInput.js';
import { AutomationInput } from './automation/input.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';

/** 状态栏只投影已有上下文，不发请求或维护另一份连接状态。 */
class HarnessStatusbar extends Disposable {
  private readonly target = this._register(new MutableDisposable<IStatusbarEntryAccessor>());
  private readonly workspace = this._register(new MutableDisposable<IStatusbarEntryAccessor>());
  private readonly thread = this._register(new MutableDisposable<IStatusbarEntryAccessor>());
  private readonly editorLabel = this._register(new MutableDisposable());

  constructor(
    @IStatusbarService private readonly statusbar: IStatusbarService,
    @IEditorService private readonly editors: IEditorService,
  ) {
    super();
    this._register(toDisposable(workspaceContext.subscribe(() => this.update())));
    this._register(editors.onDidActiveEditorChange(() => {
      this.editorLabel.value = editors.activeEditor?.onDidChangeLabel(() => this.update());
      this.update();
    }));
    this.editorLabel.value = editors.activeEditor?.onDidChangeLabel(() => this.update());
    this.update();
  }

  private update(): void {
    const snapshot = workspaceContext.getSnapshot();
    let workspace = snapshot.workspaces.find(item => gatewayWorkspaceKey(item) === snapshot.activeId);
    // 业务编辑器优先于导航的活动工作区；设置等全局编辑器沿用导航上下文。
    const resource = EditorResourceAccessor.getCanonicalUri(this.editors.activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
    const hasOwner = this.editors.activeEditor instanceof ThreadInput || this.editors.activeEditor instanceof AutomationInput
      || ['boxteam-file', 'boxteam-change', 'boxteam-source'].includes(resource?.scheme ?? '');
    if (!snapshot.loading && !snapshot.error && snapshot.gatewayId && (workspace || hasOwner)) {
      const owner = captureSearchOwner(this.editors, this.editors.activeEditor);
      workspace = snapshot.workspaces.find(item => item.gateway_id === owner.gatewayId && item.workspace_id === owner.workspaceId);
    }
    const remoteWorkspace = workspace?.connection_kind === 'remote_gateway' ? workspace : undefined;
    const connection = remoteWorkspace?.remote_gateways.find(item => item.gateway_id === remoteWorkspace.gateway_id);
    const remote = !!remoteWorkspace;
    const label = remoteWorkspace ? connection?.name ?? remoteWorkspace.gateway_id : t('workbench.managementTarget.local');
    const error = snapshot.error ?? workspace?.connection_error;
    const targetEntry = {
      name: t('statusbar.target'), text: `${error ? '$(warning)' : snapshot.loading ? '$(loading~spin)' : remote ? '$(remote)' : '$(device-desktop)'} ${truncate(label, 24)}`,
      ariaLabel: t('statusbar.targetLabel', label),
      tooltip: error ?? t('statusbar.targetHint', label, workspace?.gateway_id ?? snapshot.gatewayId ?? ''),
      command: 'boxteam.gateway.menu', kind: error ? 'warning' as const : remote ? 'remote' as const : 'standard' as const,
    };
    if (this.target.value) this.target.value.update(targetEntry);
    else this.target.value = this.statusbar.addEntry(targetEntry, 'boxteam.status.target', StatusbarAlignment.LEFT, 1000);

    if (workspace) {
      const owner = { gatewayId: workspace.gateway_id, workspaceId: workspace.workspace_id };
      const entry = {
        name: t('statusbar.workspace'), text: `$(folder) ${truncate(workspace.name, 28)}`,
        ariaLabel: t('statusbar.workspaceLabel', workspace.name, workspace.status),
        tooltip: t('statusbar.workspaceHint', workspace.name, workspace.status, workspace.workspace_id),
        command: { id: 'boxteam.workspace.details', title: t('statusbar.workspace'), arguments: [owner] },
        kind: workspace.status === 'ready' ? 'standard' as const : 'warning' as const,
      };
      if (this.workspace.value) this.workspace.value.update(entry);
      else this.workspace.value = this.statusbar.addEntry(entry, 'boxteam.status.workspace', StatusbarAlignment.LEFT, 100);
    } else this.workspace.clear();

    const input = this.editors.activeEditor;
    if (input instanceof ThreadInput) {
      const entry = {
        name: t('statusbar.thread'), text: `$(comment-discussion) ${truncate(input.getName(), 24)}`,
        ariaLabel: t('statusbar.threadLabel', input.getName()),
        tooltip: t('statusbar.threadHint', input.getName(), input.owner.sessionId, input.owner.threadId),
        command: 'boxteam.quickAccess.threads',
      };
      if (this.thread.value) this.thread.value.update(entry);
      else this.thread.value = this.statusbar.addEntry(entry, 'boxteam.status.thread', StatusbarAlignment.RIGHT, 10);
    } else this.thread.clear();
  }
}

registerWorkbenchContribution2('boxteam.statusbar', HarnessStatusbar, WorkbenchPhase.AfterRestored);
