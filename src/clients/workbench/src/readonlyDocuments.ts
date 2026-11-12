import { basename } from '../upstream/src/vs/base/common/resources.js';
import { URI, type UriComponents } from '../upstream/src/vs/base/common/uri.js';
import { EditorExtensions, type IEditorFactoryRegistry, type IEditorSerializer, type IUntypedFileEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { TextResourceEditorInput } from '../upstream/src/vs/workbench/common/editor/textResourceEditorInput.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { ITextEditorService } from '../upstream/src/vs/workbench/services/textfile/common/textEditorService.js';
import type { SessionFileChangeDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ThreadOwner } from './agentApi.js';
import { sourceDocumentSource } from './sourceDocuments.js';

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

/** 保留无法解析的原展示记录；没有资源目标，不进行路由或正文读取。 */
export class UnavailableReadonlyDocumentInput extends EditorInput {
  readonly resource = undefined;
  constructor(readonly stored: unknown, readonly reason: string, private readonly title: string) { super(); }
  get typeId(): string { return TextResourceEditorInput.ID; }
  override getName(): string { return this.title; }
}

/** 仅持久化项目的只读引用；正文与每组视图态仍归原模型／编辑器组。 */
export class ReadonlyDocumentSerializer implements IEditorSerializer {
  canSerialize(input: EditorInput): boolean {
    return input instanceof UnavailableReadonlyDocumentInput
      || input instanceof TextResourceEditorInput && ['boxteam-change', 'boxteam-source'].includes(input.resource.scheme);
  }
  serialize(input: EditorInput): string | undefined {
    if (input instanceof UnavailableReadonlyDocumentInput) return JSON.stringify({ version: 1, unavailable: input.stored,
      reason: input.reason, name: input.getName() });
    if (!this.canSerialize(input)) return undefined;
    return JSON.stringify({ version: 1, resource: input.resource!.toJSON(), name: input.getName(), description: input.getDescription() });
  }
  deserialize(instantiation: IInstantiationService, value: string): EditorInput {
    let parsed: unknown;
    try { parsed = JSON.parse(value); }
    catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      return new UnavailableReadonlyDocumentInput(value, `只读文档恢复 JSON 损坏：${error.message}`, '无法恢复的只读文档');
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return new UnavailableReadonlyDocumentInput(parsed, '只读文档恢复记录必须是对象', '无法恢复的只读文档');
    }
    const stored = parsed as { version?: number; resource?: UriComponents; name?: string; description?: string;
      unavailable?: unknown; reason?: string };
    const name = typeof stored.name === 'string' && stored.name ? stored.name : '无法恢复的只读文档';
    const unavailable = (reason: string) => new UnavailableReadonlyDocumentInput(parsed, reason, name);
    if (stored.version !== 1) return unavailable(`不支持的只读文档恢复版本：${String(stored.version)}`);
    if (Object.hasOwn(stored, 'unavailable')) return new UnavailableReadonlyDocumentInput(stored.unavailable,
      typeof stored.reason === 'string' ? stored.reason : '只读文档记录无法恢复', name);
    if (!stored.resource || typeof stored.resource.scheme !== 'string' || typeof stored.resource.path !== 'string') {
      return unavailable('只读文档恢复记录缺少资源身份');
    }
    let resource: URI;
    try {
      resource = URI.revive(stored.resource);
      if (!['boxteam-change', 'boxteam-source'].includes(resource.scheme)) return unavailable('只读文档恢复记录不是项目资源');
      resource = normalizeReadonlyInput({ resource }).resource!;
      if (resource.scheme === 'boxteam-change') changeFileSource(resource);
      else sourceDocumentSource(resource);
    } catch (error) {
      if (!(error instanceof Error)) throw error;
      return unavailable(`只读文档恢复引用无效：${error.message}`);
    }
    return instantiation.invokeFunction(accessor => accessor.get(ITextEditorService).createTextEditor({ resource,
      label: name, description: typeof stored.description === 'string' ? stored.description : undefined }));
  }
}

export function installReadonlyDocumentSerialization(): void {
  Registry.as<IEditorFactoryRegistry>(EditorExtensions.EditorFactory).registerEditorSerializer(TextResourceEditorInput.ID, ReadonlyDocumentSerializer);
}
