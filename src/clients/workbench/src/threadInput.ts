import { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { IEditorSerializer, IUntypedEditorInput } from '../upstream/src/vs/workbench/common/editor.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import type { ThreadOwner } from './agentApi.js';
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

export class ThreadInput extends EditorInput {
  static readonly ID = 'boxteam.editor.thread';
  readonly resource: URI;
  restorable = true;
  resourcesVisible = false;
  resourceTab: ThreadResourceTab = 'files';
  resourceRatio = 0.42;
  filePreviewLayout: WebUiLayoutSettings = {};
  fileTreeExpandedPaths: string[] = [];
  constructor(readonly owner: ThreadOwner) {
    super();
    this.resource = URI.from({ scheme: 'boxteam-thread', authority: owner.workspaceId,
      path: `/${owner.sessionId}/${owner.threadId}`, query: new URLSearchParams({ route: owner.routeId }).toString() });
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
    return other instanceof ThreadInput && this.resource.toString() === other.resource.toString();
  }
}

export class ThreadSerializer implements IEditorSerializer {
  canSerialize(input: EditorInput): input is ThreadInput { return input instanceof ThreadInput && input.restorable; }
  serialize(input: EditorInput): string | undefined {
    return this.canSerialize(input) ? JSON.stringify({ owner: input.owner, ...input.getViewState() }) : undefined;
  }
  deserialize(_instantiation: IInstantiationService, serialized: string): ThreadInput {
    const data = JSON.parse(serialized) as { owner: ThreadOwner; resourcesVisible: boolean; resourceTab?: ThreadResourceTab; resourceRatio?: number };
    if (!data.owner.workspaceId || !data.owner.routeId || !data.owner.sessionId || !data.owner.threadId) throw new Error('Thread 编辑器恢复数据缺少精确 owner');
    const input = new ThreadInput(data.owner);
    input.restoreViewState(data);
    return input;
  }
}
