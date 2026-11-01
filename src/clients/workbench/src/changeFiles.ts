import { URI } from '../upstream/src/vs/base/common/uri.js';
import { Event } from '../upstream/src/vs/base/common/event.js';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IFileService, FileSystemProviderCapabilities, IFileSystemProviderWithFileReadWriteCapability, IStat, FileType, FilePermission, createFileSystemProviderError, FileSystemProviderErrorCode } from '../upstream/src/vs/platform/files/common/files.js';
import type { SessionChangesetDTO, SessionFileChangeDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway, ThreadOwner } from './agentApi.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';

const scheme = 'boxteam-change';
type Phase = 'before' | 'after' | 'patch';
interface ChangeSource { owner: ThreadOwner; changesetId: string; filePath: string; editId: string; phase: Phase }

export function changeFileUri(owner: ThreadOwner, changesetId: string, file: Pick<SessionFileChangeDTO, 'file_path' | 'latest_edit_id'>, phase: Phase): URI {
  const source: ChangeSource = { owner, changesetId, filePath: file.file_path, editId: file.latest_edit_id, phase };
  return URI.from({ scheme, authority: owner.workspaceId, path: `/${owner.sessionId}/${changesetId}/${phase}/${file.file_path.split('/').pop()}${phase === 'patch' ? '.diff' : ''}`, query: JSON.stringify(source) });
}

class ChangeFiles implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive | FileSystemProviderCapabilities.Readonly;
  readonly onDidChangeCapabilities = Event.None;
  readonly onDidChangeFile = Event.None;
  watch() { return Disposable.None; }
  private async snapshot(resource: URI): Promise<{ bytes: Uint8Array; modifiedAt: number }> {
    const source = JSON.parse(resource.query) as ChangeSource;
    if (source.owner.workspaceId !== resource.authority || !['before', 'after', 'patch'].includes(source.phase)) throw new Error('更改文档的 owner 或阶段无效');
    await gateway.assertWorkspace(source.owner);
    const data = await gateway.request<SessionChangesetDTO>(`/api/v1/sessions/${encodeURIComponent(source.owner.sessionId)}/changesets/${encodeURIComponent(source.changesetId)}`, {}, source.owner.routeId);
    if (data.session_id !== source.owner.sessionId || data.changeset_id !== source.changesetId) throw new Error('更改响应 owner 不匹配');
    const file = data.files.find(file => file.file_path === source.filePath);
    if (!file || file.latest_edit_id !== source.editId) throw new Error('此更改版本已变化，请重新从会话更改列表打开');
    const content = source.phase === 'patch' ? file.diff_text : source.phase === 'before' ? file.before_preview : file.after_preview;
    const absent = source.phase === 'before' && file.kind === 'create' || source.phase === 'after' && file.kind === 'delete';
    if (content == null && !absent) throw new Error('后端未提供更改预览');
    const modifiedAt = Date.parse(data.generated_at ?? '');
    if (!Number.isFinite(modifiedAt)) throw new Error('更改响应缺少有效生成时间');
    return { bytes: new TextEncoder().encode(content ?? ''), modifiedAt };
  }
  async stat(resource: URI): Promise<IStat> {
    const { bytes, modifiedAt } = await this.snapshot(resource);
    return { type: FileType.File, size: bytes.length, mtime: modifiedAt, ctime: modifiedAt, permissions: FilePermission.Readonly };
  }
  async readFile(resource: URI): Promise<Uint8Array> { return (await this.snapshot(resource)).bytes; }
  async writeFile(): Promise<void> { throw createFileSystemProviderError('更改快照只读', FileSystemProviderErrorCode.NoPermissions); }
  async mkdir(): Promise<void> { throw createFileSystemProviderError('更改快照只读', FileSystemProviderErrorCode.NoPermissions); }
  async delete(): Promise<void> { throw createFileSystemProviderError('更改快照只读', FileSystemProviderErrorCode.NoPermissions); }
  async rename(): Promise<void> { throw createFileSystemProviderError('更改快照只读', FileSystemProviderErrorCode.NoPermissions); }
  async readdir(): Promise<[string, FileType][]> { throw createFileSystemProviderError('更改快照不是目录', FileSystemProviderErrorCode.FileNotADirectory); }
}

export function installChangeFiles(files: IFileService): void { files.registerProvider(scheme, new ChangeFiles()); }

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
    const source = JSON.parse(resource.query) as ChangeSource;
    await editors.openEditor({ resource: changeFileUri(source.owner, source.changesetId,
      { file_path: source.filePath, latest_edit_id: source.editId }, 'patch'), options: { pinned: true } });
  }
});
