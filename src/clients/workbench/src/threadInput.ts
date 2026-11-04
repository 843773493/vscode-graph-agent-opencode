import { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { IEditorSerializer, IUntypedEditorInput, isResourceEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { gateway, type ThreadOwner } from './agentApi.js';
import type { IResourceEditorInput } from '../upstream/src/vs/platform/editor/common/editor.js';
import type { WebUiLayoutSettings } from '../../web/src/types/backend.js';

export type ThreadResourceTab = 'files' | 'changes' | 'tasks' | 'connections' | 'debug';
interface ThreadViewState {
  resourcesVisible: boolean;
  resourceTab: ThreadResourceTab;
  resourceRatio: number;
  filePreviewLayout: WebUiLayoutSettings;
  fileTreeExpandedPaths: string[];
}

type ThreadTarget = Omit<ThreadOwner, 'workspaceKey' | 'title'>;
interface StoredThread {
  version: 2;
  target: ThreadTarget;
  title: string;
  viewState: ThreadViewState;
}

/** 缺失归属的旧展示记录仍可关闭／移除，不向当前 Gateway 猜测请求。 */
export class UnavailableThreadInput extends EditorInput {
  readonly resource = undefined;
  constructor(readonly stored: object, readonly reason: string, private readonly title: string) { super(); }
  get typeId(): string { return ThreadInput.ID; }
  override get editorId(): string { return ThreadInput.ID; }
  override getName(): string { return this.title; }
}

export class ThreadInput extends EditorInput {
  static readonly ID = 'boxteam.editor.thread';
  readonly resource: URI;
  restorable = true;
  resourcesVisible = false;
  resourceTab: ThreadResourceTab = 'files';
  resourceRatio = 0.42;
  filePreviewLayout: WebUiLayoutSettings = {};
  fileTreeExpandedPaths: string[] = [];
  private resolving?: Promise<ThreadOwner>;
  constructor(readonly owner: ThreadOwner) {
    super();
    this.resource = URI.from({ scheme: 'boxteam-thread', authority: owner.gatewayId,
      path: `/${owner.workspaceId}/${owner.sessionId}/${owner.threadId}` });
  }
  static fromResource(resource: URI, title?: string, viewState?: object): ThreadInput {
    const [workspaceId, sessionId, threadId, extra] = resource.path.slice(1).split('/');
    if (resource.scheme !== 'boxteam-thread' || !resource.authority || !workspaceId || !sessionId || !threadId
      || extra !== undefined || resource.query || resource.fragment) throw new Error('Thread 编辑器资源缺少稳定 Gateway／工作区／Session／Thread 身份，或包含过时路由');
    const input = new ThreadInput({ gatewayId: resource.authority, workspaceId, sessionId, threadId, workspaceKey: '', title: title ?? threadId });
    input.restoreViewState(viewState);
    return input;
  }
  async resolveOwner(): Promise<ThreadOwner> {
    if (!this.resolving) {
      this.resolving = gateway.resolveWorkspace(this.owner).then(workspaceKey => {
        this.owner.workspaceKey = workspaceKey;
        return this.owner;
      }).finally(() => { this.resolving = undefined; });
    }
    return this.resolving;
  }
  get typeId(): string { return ThreadInput.ID; }
  override get editorId(): string { return ThreadInput.ID; }
  override getName(): string { return this.owner.title; }
  setTitle(title: string): void { if (this.owner.title !== title) { this.owner.title = title; this._onDidChangeLabel.fire(); } }
  override getDescription(): string { return `${this.owner.sessionId} / ${this.owner.threadId}`; }
  override copy(): ThreadInput {
    // 原版分屏复制输入；每个组持有自己的展示态，业务 owner 保持相同。
    const input = new ThreadInput({ ...this.owner });
    input.restorable = this.restorable;
    input.restoreViewState(structuredClone(this.getViewState()));
    return input;
  }
  getViewState(): ThreadViewState {
    return { resourcesVisible: this.resourcesVisible, resourceTab: this.resourceTab, resourceRatio: this.resourceRatio,
      filePreviewLayout: this.filePreviewLayout, fileTreeExpandedPaths: this.fileTreeExpandedPaths };
  }
  restoreViewState(viewState: object | undefined): void {
    const state = viewState as Partial<ThreadViewState> | undefined;
    this.resourcesVisible = state?.resourcesVisible ?? false;
    this.resourceTab = state?.resourceTab ?? 'files';
    this.resourceRatio = state?.resourceRatio ?? 0.42;
    this.filePreviewLayout = state?.filePreviewLayout ?? {};
    this.fileTreeExpandedPaths = state?.fileTreeExpandedPaths ?? [];
  }
  override toUntyped(): IResourceEditorInput {
    return { resource: this.resource, label: this.getName(), options: { override: ThreadInput.ID, viewState: this.getViewState() } };
  }
  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    const resource = other instanceof ThreadInput ? other.resource : isResourceEditorInput(other) ? other.resource : undefined;
    return resource?.toString() === this.resource.toString();
  }
}

export class ThreadSerializer implements IEditorSerializer {
  canSerialize(input: EditorInput): input is ThreadInput | UnavailableThreadInput {
    return input instanceof UnavailableThreadInput || input instanceof ThreadInput && input.restorable;
  }
  serialize(input: EditorInput): string | undefined {
    if (input instanceof UnavailableThreadInput) return JSON.stringify({ version: 2, unavailable: input.stored, reason: input.reason, title: input.getName() });
    if (!(input instanceof ThreadInput) || !input.restorable) return undefined;
    const { gatewayId, workspaceId, sessionId, threadId, title } = input.owner;
    const stored: StoredThread = { version: 2, target: { gatewayId, workspaceId, sessionId, threadId }, title, viewState: input.getViewState() };
    return JSON.stringify(stored);
  }
  deserialize(_instantiation: IInstantiationService, serialized: string): ThreadInput | UnavailableThreadInput {
    const data = JSON.parse(serialized) as StoredThread & { unavailable?: object; reason?: string; owner?: ThreadOwner };
    // TODO：仅在一次展示格式迁移时识别无版本旧记录；缺少 Gateway 身份无法安全恢复。
    if (data.version === undefined && data.owner) return new UnavailableThreadInput(data,
      '旧 Thread 记录缺少 Gateway 身份，无法自动恢复；请从会话导航重新打开，或关闭此记录。', data.owner.title);
    if (data.version !== 2) throw new Error(`不支持的 Thread 编辑器恢复版本：${String(data.version)}`);
    if (data.unavailable) return new UnavailableThreadInput(data.unavailable, data.reason ?? 'Thread 记录无法恢复', data.title);
    if (!data.target || Object.values(data.target).some(value => typeof value !== 'string' || !value.trim())
      || !data.target.gatewayId || !data.target.workspaceId || !data.target.sessionId || !data.target.threadId) throw new Error('Thread 编辑器恢复数据缺少精确 owner');
    const input = new ThreadInput({ ...data.target, title: data.title, workspaceKey: '' });
    input.restoreViewState(data.viewState);
    return input;
  }
}
