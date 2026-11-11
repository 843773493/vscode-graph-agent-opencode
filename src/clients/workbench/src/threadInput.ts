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
  selectedChangesetId?: string;
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
  constructor(readonly stored: unknown, readonly reason: string, private readonly title: string) { super(); }
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
  selectedChangesetId?: string;
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
    return { resourcesVisible: this.resourcesVisible, resourceTab: this.resourceTab, selectedChangesetId: this.selectedChangesetId, resourceRatio: this.resourceRatio,
      filePreviewLayout: this.filePreviewLayout, fileTreeExpandedPaths: this.fileTreeExpandedPaths };
  }
  restoreViewState(viewState: object | undefined): void {
    const state = viewState as Partial<ThreadViewState> | undefined;
    this.resourcesVisible = state?.resourcesVisible ?? false;
    this.resourceTab = state?.resourceTab ?? 'files';
    this.selectedChangesetId = state?.selectedChangesetId;
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
    let parsed: unknown;
    try { parsed = JSON.parse(serialized); }
    catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      return new UnavailableThreadInput(serialized, `Thread 编辑器恢复 JSON 损坏：${error.message}`, '无法恢复的 Thread');
    }
    // 原生组恢复会调用此 serializer；损坏的一条记录不能使整组恢复失败。
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return new UnavailableThreadInput(parsed, 'Thread 编辑器恢复记录必须是对象', '无法恢复的 Thread');
    }
    const data = parsed as Partial<StoredThread> & { unavailable?: unknown; reason?: string; owner?: ThreadOwner };
    const title = typeof data.title === 'string' && data.title ? data.title
      : typeof data.owner?.title === 'string' && data.owner.title ? data.owner.title : '无法恢复的 Thread';
    // 缺少来源的旧记录保留原件；显式导入恢复材料也不能猜当前 Gateway。
    if (data.version === undefined && data.owner) return new UnavailableThreadInput(data,
      '旧 Thread 记录尚未确认 Gateway 身份；可运行“导入工作区身份恢复材料”重试，或从会话导航重新打开。', title);
    if (data.version !== 2) return new UnavailableThreadInput(data, `不支持的 Thread 编辑器恢复版本：${String(data.version)}`, title);
    if (Object.hasOwn(data, 'unavailable')) return new UnavailableThreadInput(data.unavailable, data.reason ?? 'Thread 记录无法恢复', title);
    if (!data.target || Object.values(data.target).some(value => typeof value !== 'string' || !value.trim())
      || !data.target.gatewayId || !data.target.workspaceId || !data.target.sessionId || !data.target.threadId) {
      return new UnavailableThreadInput(data, 'Thread 编辑器恢复数据缺少精确 owner', title);
    }
    const input = new ThreadInput({ ...data.target, title, workspaceKey: '' });
    input.restoreViewState(data.viewState);
    return input;
  }
}
