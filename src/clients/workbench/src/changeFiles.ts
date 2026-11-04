import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { ILanguageService } from '../upstream/src/vs/editor/common/languages/language.js';
import { IModelService } from '../upstream/src/vs/editor/common/services/model.js';
import { ITextModelService, type ITextModelContentProvider } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import type { SessionChangesetDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gatewayWorkspaceRequestHeaders } from '../../web/src/api/http.js';
import { gateway } from './agentApi.js';
import { changeFileSource, changeFileUri, type ChangeSource } from './readonlyDocuments.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';

const scheme = 'boxteam-change';
/** 原 resolver 共享并释放不可变模型；只合并在途读取，不保存第二份历史正文。 */
export class ChangeDocuments extends Disposable implements ITextModelContentProvider {
  private readonly pending = new Map<string, Promise<SessionChangesetDTO>>();
  constructor(
    @ITextModelService resolver: ITextModelService,
    @IModelService private readonly models: IModelService,
    @ILanguageService private readonly languages: ILanguageService,
  ) {
    super();
    this._register(resolver.registerTextModelContentProvider(scheme, this));
  }

  private changeset(source: ChangeSource): Promise<SessionChangesetDTO> {
    const { owner, changesetId } = source;
    const key = JSON.stringify([owner.gatewayId, owner.workspaceId, owner.sessionId, changesetId]);
    let pending = this.pending.get(key);
    if (!pending) {
      pending = (async () => {
        const target = await gateway.resolveWorkspaceTarget(owner);
        if (target.status !== 'ready') throw new Error(`更改工作区当前不可用（${target.status}），请明确启动或连接`);
        const data = await gateway.request<SessionChangesetDTO>(`/api/v1/sessions/${encodeURIComponent(owner.sessionId)}/changesets/${encodeURIComponent(changesetId)}`,
          { headers: gatewayWorkspaceRequestHeaders(target) }, target.workspace_id);
        if (data.session_id !== owner.sessionId || data.changeset_id !== changesetId) throw new Error('更改响应 owner 不匹配');
        return data;
      })().finally(() => this.pending.delete(key));
      this.pending.set(key, pending);
    }
    return pending;
  }

  async provideTextContent(resource: URI) {
    const existing = this.models.getModel(resource);
    if (existing) return existing;
    const source = changeFileSource(resource);
    const data = await this.changeset(source);
    const file = data.files.find(file => file.file_path === source.filePath);
    if (!file || file.latest_edit_id !== source.editId) throw new Error('此更改版本已变化，请重新从会话更改列表打开');
    const content = source.phase === 'patch' ? file.diff_text : source.phase === 'before' ? file.before_preview : file.after_preview;
    const absent = source.phase === 'before' && file.kind === 'create' || source.phase === 'after' && file.kind === 'delete';
    if (content == null && !absent) throw new Error('后端未提供更改预览');
    return this.models.createModel(content ?? '', this.languages.createByFilepathOrFirstLine(resource), resource);
  }
}

registerWorkbenchContribution2('boxteam.changeDocuments', ChangeDocuments, WorkbenchPhase.BlockRestore);

// 原生差异编辑器读取前后预览；完整 unified diff 仍由同一后端变更提供。
registerAction2(class OpenChangePatch extends Action2 {
  constructor() {
    super({ id: 'boxteam.changes.openPatch', title: { value: '打开完整补丁', original: 'Open Full Patch' },
      icon: Codicon.diff, f1: true, precondition: ContextKeyExpr.equals('resourceScheme', scheme),
      menu: { id: MenuId.EditorTitle, group: 'navigation', when: ContextKeyExpr.equals('resourceScheme', scheme) } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const editors = accessor.get(IEditorService);
    const resource = EditorResourceAccessor.getCanonicalUri(editors.activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (!resource || resource.scheme !== scheme) throw new Error('当前编辑器不是会话文件更改');
    const source = changeFileSource(resource);
    await editors.openEditor({ resource: changeFileUri(source.owner, source.changesetId,
      { file_path: source.filePath, latest_edit_id: source.editId }, 'patch'), options: { pinned: true } });
  }
});
