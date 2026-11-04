import { basename } from '../upstream/src/vs/base/common/resources.js';
import { URI, type UriComponents } from '../upstream/src/vs/base/common/uri.js';
import { EditorExtensions, type IEditorFactoryRegistry, type IEditorSerializer, type IUntypedFileEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import type { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { TextResourceEditorInput } from '../upstream/src/vs/workbench/common/editor/textResourceEditorInput.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { ITextEditorService } from '../upstream/src/vs/workbench/services/textfile/common/textEditorService.js';
import type { SessionFileChangeDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ThreadOwner } from './agentApi.js';

type Phase = 'before' | 'after' | 'patch';
type ChangeOwner = Pick<ThreadOwner, 'gatewayId' | 'workspaceId' | 'sessionId'>;
export interface ChangeSource { owner: ChangeOwner; changesetId: string; filePath: string; editId: string; phase: Phase }

/** 更改的内部投影只含稳定 owner 和原版本；规范文件 URI 不由客户端重新解释。 */
export function changeFileUri(owner: ChangeOwner, changesetId: string, file: Pick<SessionFileChangeDTO, 'file_path' | 'latest_edit_id'>, phase: Phase): URI {
  const name = basename(URI.parse(file.file_path)) + (phase === 'patch' ? '.diff' : '');
  return URI.from({ scheme: 'boxteam-change', authority: owner.gatewayId, path: '/' + [owner.workspaceId, owner.sessionId,
    changesetId, file.latest_edit_id, phase, file.file_path, name].map(encodeURIComponent).join('/') });
}

export function changeFileSource(resource: URI): ChangeSource {
  const parts = resource.path.slice(1).split('/').map(decodeURIComponent);
  if (resource.scheme !== 'boxteam-change' || !resource.authority || parts.length !== 7 || parts.some(part => !part)
    || !['before', 'after', 'patch'].includes(parts[4]) || resource.query || resource.fragment) {
    throw new Error('更改编辑器记录缺少稳定身份或包含过时路由，请从会话更改列表重新打开');
  }
  const [workspaceId, sessionId, changesetId, editId, phase, filePath] = parts;
  return { owner: { gatewayId: resource.authority, workspaceId, sessionId }, changesetId, editId, phase: phase as Phase, filePath };
}

/** 原输入工厂统一迁移旧展示引用；后续只保存新投影，不保留旧读取 Provider。 */
type TextInput = Omit<IUntypedFileEditorInput, 'forceFile'> & { forceFile?: true };
export function normalizeReadonlyInput(input: TextInput): TextInput {
  let resource = input.resource;
  if (resource?.scheme === 'boxteam-change' && resource.query) {
    // TODO：一次展示格式迁移；无稳定 Gateway 身份的旧引用只能明确重新打开，不能猜当前目标。
    const source = JSON.parse(resource.query) as ChangeSource;
    if (!source.owner?.gatewayId || source.owner.workspaceId !== resource.authority || !source.owner.sessionId
      || !source.changesetId || !source.editId || !source.filePath || !['before', 'after', 'patch'].includes(source.phase)) {
      throw new Error('旧更改记录缺少可核验的稳定身份，请从会话更改列表重新打开或移除此记录');
    }
    resource = changeFileUri(source.owner, source.changesetId, { file_path: source.filePath, latest_edit_id: source.editId }, source.phase);
  }
  return resource?.scheme === 'boxteam-change' || resource?.scheme === 'boxteam-source' ? { ...input, resource, forceFile: undefined } : input;
}

/** 仅持久化项目的只读引用；正文与每组视图态仍归原模型／编辑器组。 */
export class ReadonlyDocumentSerializer implements IEditorSerializer {
  canSerialize(input: EditorInput): boolean {
    return input instanceof TextResourceEditorInput && ['boxteam-change', 'boxteam-source'].includes(input.resource.scheme);
  }
  serialize(input: EditorInput): string | undefined {
    if (!this.canSerialize(input)) return undefined;
    return JSON.stringify({ version: 1, resource: input.resource!.toJSON(), name: input.getName(), description: input.getDescription() });
  }
  deserialize(instantiation: IInstantiationService, value: string): EditorInput {
    const stored = JSON.parse(value) as { version: number; resource: UriComponents; name: string; description?: string };
    if (stored.version !== 1) throw new Error(`不支持的只读文档恢复版本：${stored.version}`);
    const resource = URI.revive(stored.resource);
    if (!['boxteam-change', 'boxteam-source'].includes(resource.scheme)) throw new Error('只读文档恢复记录不是项目资源');
    return instantiation.invokeFunction(accessor => accessor.get(ITextEditorService).createTextEditor({ resource,
      label: stored.name, description: stored.description }));
  }
}

export function installReadonlyDocumentSerialization(): void {
  Registry.as<IEditorFactoryRegistry>(EditorExtensions.EditorFactory).registerEditorSerializer(TextResourceEditorInput.ID, ReadonlyDocumentSerializer);
}
