import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { t } from '../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey } from "../../web/src/api/workspaceIdentity.js";
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { ILanguageService } from '../upstream/src/vs/editor/common/languages/language.js';
import { IModelService } from '../upstream/src/vs/editor/common/services/model.js';
import { ITextModelService, type ITextModelContentProvider } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import type { SessionChangesetDTO, SessionFileChangeDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gatewayWorkspaceRequestHeaders, onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { gateway } from './agentApi.js';
import { workspaceContext } from './workspaceContext.js';
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
  static readonly ID = 'boxteam.changeDocuments';
  private readonly pending = new Map<string, { promise: Promise<SessionChangesetDTO>; readers: number; signal: AbortSignal; release(): void }>();
  constructor(
    @ITextModelService resolver: ITextModelService,
    @IModelService private readonly models: IModelService,
    @ILanguageService private readonly languages: ILanguageService,
  ) {
    super();
    this._register(resolver.registerTextModelContentProvider(scheme, this));
  }

  async withFile<T>(source: Pick<ChangeSource, 'owner' | 'changesetId' | 'filePath'>, use: (file: SessionFileChangeDTO) => T | Promise<T>): Promise<T> {
    const { owner, changesetId } = source;
    const key = JSON.stringify([owner.gatewayId, owner.workspaceId, owner.sessionId, changesetId]);
    let pending = this.pending.get(key);
    if (!pending || pending.signal.aborted) {
      const controller = new AbortController();
      const invalidate = () => controller.abort(new Error(t('quickAccess.resource.identityChanged')));
      const releaseToken = onGatewayTokenInvalidated(0, invalidate);
      let gatewayId = workspaceContext.getSnapshot().gatewayId;
      const releaseGateway = workspaceContext.subscribe(() => {
        const current = workspaceContext.getSnapshot().gatewayId;
        if (gatewayId && current !== gatewayId) invalidate();
        gatewayId = current;
      });
      const promise = (async () => {
        const target = await gateway.resolveWorkspaceTarget(owner, controller.signal);
        controller.signal.throwIfAborted();
        if (target.status !== 'ready') throw new Error(t('workbench.changes.workspaceUnavailable', target.status));
        const data = await gateway.request<SessionChangesetDTO>(`/api/v1/sessions/${encodeURIComponent(owner.sessionId)}/changesets/${encodeURIComponent(changesetId)}`,
          { signal: controller.signal, headers: gatewayWorkspaceRequestHeaders(target) }, gatewayWorkspaceKey(target));
        controller.signal.throwIfAborted();
        if (data.session_id !== owner.sessionId || data.changeset_id !== changesetId) throw new Error(t('workbench.changes.ownerMismatch'));
        return data;
      })();
      pending = { promise, readers: 0, signal: controller.signal, release() { releaseToken(); releaseGateway(); } };
      this.pending.set(key, pending);
    }
    pending.readers++;
    try {
      const data = await pending.promise;
      pending.signal.throwIfAborted();
      const file = data.files.find(file => file.file_path === source.filePath);
      if (!file) throw new Error(t('workbench.changes.fileRemoved'));
      // URI 打开与前后模型在同一次读取作用域内消费；结清即释放，不缓存第二份正文。
      const result = await use(file);
      pending.signal.throwIfAborted();
      return result;
    } finally {
      pending.readers--;
      if (pending.readers === 0) {
        if (this.pending.get(key) === pending) this.pending.delete(key);
        pending.release();
      }
    }
  }

  async provideTextContent(resource: URI) {
    const existing = this.models.getModel(resource);
    if (existing) return existing;
    const source = changeFileSource(resource);
    return this.withFile(source, file => {
      if (file.latest_edit_id !== source.editId) throw new Error(t('workbench.changes.versionChanged'));
      const content = source.phase === 'patch' ? file.diff_text : source.phase === 'before' ? file.before_preview : file.after_preview;
      const absent = source.phase === 'before' && file.kind === 'create' || source.phase === 'after' && file.kind === 'delete';
      if (content == null && !absent) throw new Error(t('workbench.changes.previewMissing'));
      return this.models.createModel(content ?? '', this.languages.createByFilepathOrFirstLine(resource), resource);
    });
  }
}

registerWorkbenchContribution2(ChangeDocuments.ID, ChangeDocuments, WorkbenchPhase.BlockRestore);

// 原生差异编辑器读取前后预览；完整 unified diff 仍由同一后端变更提供。
registerAction2(class OpenChangePatch extends Action2 {
  constructor() {
    super({ id: 'boxteam.changes.openPatch', title: { value: t('workbench.changes.openPatch'), original: actionTitles['workbench.changes.openPatch'] },
      icon: Codicon.diff, f1: true, precondition: ContextKeyExpr.equals('resourceScheme', scheme),
      menu: { id: MenuId.EditorTitle, group: 'navigation', when: ContextKeyExpr.equals('resourceScheme', scheme) } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const editors = accessor.get(IEditorService);
    const resource = EditorResourceAccessor.getCanonicalUri(editors.activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (!resource || resource.scheme !== scheme) throw new Error(t('workbench.changes.editorRequired'));
    const source = changeFileSource(resource);
    await editors.openEditor({ resource: changeFileUri(source.owner, source.changesetId,
      { file_path: source.filePath, latest_edit_id: source.editId }, 'patch'), options: { pinned: true } });
  }
});
