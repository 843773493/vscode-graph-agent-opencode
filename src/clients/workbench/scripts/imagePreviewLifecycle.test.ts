import { afterEach, expect, spyOn, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import ts from 'typescript';
import { RunOnceScheduler } from '../upstream/src/vs/base/common/async.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore, MutableDisposable, toDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { FileChangeType } from '../upstream/src/vs/platform/files/common/files.js';
import { invalidateGatewayUserSession, onGatewayUserSessionInvalidated } from '../../web/src/api/http.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const path = resolve(root, 'upstream/src/vs/workbench/contrib/mediaPreview/browser/imagePreviewEditor.ts');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const editorSource = ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
const viewerPath = resolve(root, 'upstream/src/vs/workbench/contrib/mediaPreview/browser/imageViewer.ts');
const viewerSource = ts.createSourceFile(viewerPath, await readFile(viewerPath, 'utf8'), ts.ScriptTarget.Latest, true);
function declaration(source: ts.SourceFile, name: string): ts.ClassDeclaration {
  const node = source.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === name);
  if (!node || !ts.isClassDeclaration(node)) throw new Error(`缺少原类：${name}`);
  return node;
}
function execute<T>(body: string, name: string, bindings: Record<string, unknown>): T {
  const compiled = ts.transpile(body, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext });
  return new Function(...Object.keys(bindings), `${compiled}\nreturn ${name};`)(...Object.values(bindings)) as T;
}

class Input {
  constructor(readonly resource: URI) {}
  getName() { return 'image.png'; }
}
class Pane extends Disposable {
  input?: Input;
  group = {};
  async setInput(input: Input, _options?: object, _context?: object, _token?: CancellationToken) { this.input = input; }
  clearInput() { this.input = undefined; }
}
class ViewerSurface extends Disposable {
  _sourceRequestId = 0;
  _hasImage = false;
  _image = { src: '', alt: '', removeAttribute: () => { this._image.src = ''; } };
  _scale = 'fit';
  element = { classList: { toggle() {}, remove() {} }, scrollTo() {}, setAttribute() {}, scrollLeft: 0, scrollTop: 0 };
  _applyScale() {}
  _hasIntrinsicWidth() { return true; }
}
interface Viewer extends ViewerSurface {
  hasImage: boolean;
  clear(): void;
  setSource(src: string, options: object): Promise<boolean>;
}
// 保留上游真实解码代际与 clear；只替换无关的 DOM 布局和服务构造。
const viewerMembers = declaration(viewerSource, 'ImageViewer').members.filter(node =>
  ts.isMethodDeclaration(node) && ['setSource', 'clear', 'getViewState'].includes(node.name.getText()));
const Viewer = execute<new () => Viewer>(`class Viewer extends ViewerSurface {
  get hasImage() { return this._hasImage; }
  ${viewerMembers.map(node => node.getText()).join('\n')}
}`, 'Viewer', { ViewerSurface });
interface Editor extends Pane {
  _viewer: Viewer;
  _message: { label: { textContent: string } };
  fileSize?: number;
  setInput(input: Input, options?: object, context?: object, token?: CancellationToken): Promise<void>;
  onDidChangeImageInfo(listener: () => void): { dispose(): void };
}
const editorMembers = declaration(editorSource, 'ImagePreviewEditor').members.filter(node => !ts.isConstructorDeclaration(node));
const viewStateGuard = editorSource.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'isImageViewerViewState');
const Editor = execute<new () => Editor>(`${viewStateGuard?.getText()}
class ImagePreviewEditor extends EditorPane {
  ${editorMembers.map(node => node.getText()).join('\n')}
}`, 'ImagePreviewEditor', { EditorPane: Pane, DisposableStore, MutableDisposable, Emitter, RunOnceScheduler,
  toDisposable, FileChangeType: { UPDATED: FileChangeType.UPDATED, ADDED: FileChangeType.ADDED, DELETED: FileChangeType.DELETED },
  ImagePreviewEditorInput: Input, onGatewayUserSessionInvalidated,
  isGitLfsPointer: async () => false, getMediaMime: () => 'image/png',
  localize: (_key: string, value: string, ...args: unknown[]) => value.replace(/\{(\d+)\}/g, (_match, index: string) => String(args[Number(index)])),
});

const cleanup: Array<() => void> = [];
afterEach(() => { for (const release of cleanup.splice(0).reverse()) release(); });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function flush() { for (let index = 0; index < 12; index++) await Promise.resolve(); }
function fixture(scheme = 'boxteam-file') {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'Image');
  const decodes: Array<ReturnType<typeof deferred<void>>> = [];
  Object.defineProperty(globalThis, 'Image', { configurable: true, value: class {
    src = '';
    decode() { const pending = deferred<void>(); decodes.push(pending); return pending.promise; }
  } });
  cleanup.push(() => {
    if (descriptor) Object.defineProperty(globalThis, 'Image', descriptor);
    else Reflect.deleteProperty(globalThis, 'Image');
  });
  let urls = 0;
  const createUrl = spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:image-${++urls}`);
  const revokeUrl = spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  cleanup.push(() => createUrl.mockRestore(), () => revokeUrl.mockRestore());
  const changes = new Emitter<{ contains: () => boolean }>();
  const files = { async readFile() { return { value: { buffer: new Uint8Array([1]) }, size: 1 }; } };
  const read = spyOn(files, 'readFile');
  let activeWatches = 0;
  const editor = new Editor();
  const viewer = new Viewer();
  Object.assign(editor, { _viewer: viewer, _message: { label: { textContent: '' } },
    _viewStateMemento: { loadEditorState() {}, saveEditorState() {} },
    _fileService: { readFile: read, onDidFilesChange: changes.event, createWatcher() {
      activeWatches++;
      return { onDidChange: changes.event, dispose() { activeWatches--; } };
    } },
  });
  cleanup.push(() => { editor.dispose(); viewer.dispose(); changes.dispose(); });
  const input = new Input(URI.parse(`${scheme}://gateway/workspace/root/image.png`));
  const load = (next = input) => editor.setInput(next, undefined, {}, CancellationToken.None);
  return { editor, viewer, input, load, decodes, read, createUrl, revokeUrl, changes, watches: () => activeWatches };
}

for (const decoding of [false, true]) test(`身份失效撤销原 Pane ${decoding ? '待解码' : '已显示'}图片及 URL`, async () => {
  const f = fixture();
  const loading = f.load();
  await flush();
  expect(f.decodes).toHaveLength(1);
  if (!decoding) { f.decodes[0]!.resolve(); await loading; }
  expect(f.viewer._image.src).toBe(decoding ? '' : 'blob:image-1');
  invalidateGatewayUserSession(8015);
  expect(f.watches()).toBe(1);
  expect(f.revokeUrl).not.toHaveBeenCalled();
  invalidateGatewayUserSession(0);
  expect(f.viewer._image.src).toBe('');
  expect(f.editor.fileSize).toBeUndefined();
  expect(f.editor._message.label.textContent).toContain('身份已改变');
  expect(f.watches()).toBe(0);
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1']]);
  f.decodes[0]!.resolve();
  await loading;
  expect(f.viewer._image.src).toBe('');
  expect(f.editor._message.label.textContent).toContain('身份已改变');
  expect(f.revokeUrl).toHaveBeenCalledTimes(1);
  // 已撤销 input 不接受 watcher 的自动重读。
  f.changes.fire({ contains: () => true });
  await new Promise(resolve => setTimeout(resolve, 130));
  expect(f.read).toHaveBeenCalledTimes(1);
});

test('迟到解码和旧 input 释放不覆盖新图，关闭后身份事件无残留订阅', async () => {
  const f = fixture();
  const oldLoad = f.load();
  await flush();
  invalidateGatewayUserSession(0);
  const nextLoad = f.load(new Input(f.input.resource.with({ path: '/workspace/root/next.png' })));
  await flush();
  f.decodes[1]!.resolve();
  await nextLoad;
  expect(f.viewer._image.src).toBe('blob:image-2');
  f.decodes[0]!.resolve();
  await oldLoad;
  expect(f.viewer._image.src).toBe('blob:image-2');
  expect(f.editor.fileSize).toBe(1);
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1']]);
  f.editor.clearInput();
  expect(f.viewer._image.src).toBe('');
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1'], ['blob:image-2']]);
  const listener = { changed() {} };
  const changed = spyOn(listener, 'changed');
  const release = f.editor.onDidChangeImageInfo(changed);
  invalidateGatewayUserSession(0);
  expect(changed).not.toHaveBeenCalled();
  release.dispose();
  expect(f.watches()).toBe(0);
});

test('原生文件不订阅工作区身份，保持原图片行为', async () => {
  const f = fixture('file');
  const loading = f.load();
  await flush();
  f.decodes[0]!.resolve();
  await loading;
  invalidateGatewayUserSession(0);
  expect(f.viewer._image.src).toBe('blob:image-1');
  expect(f.editor.fileSize).toBe(1);
  expect(f.watches()).toBe(1);
  expect(f.revokeUrl).not.toHaveBeenCalled();
  f.editor.clearInput();
  expect(f.revokeUrl).toHaveBeenCalledTimes(1);
});

test('身份失效期间读盘迟到完成，不进入 Blob 或解码阶段', async () => {
  const f = fixture();
  const content = deferred<Awaited<ReturnType<typeof f.read>>>();
  f.read.mockImplementation(() => content.promise);
  const loading = f.load();
  await flush();
  expect(f.read).toHaveBeenCalledTimes(1);
  invalidateGatewayUserSession(0);
  content.resolve({ value: { buffer: new Uint8Array([2]) }, size: 1 });
  await loading;
  expect(f.createUrl).not.toHaveBeenCalled();
  expect(f.decodes).toHaveLength(0);
  expect(f.viewer._image.src).toBe('');
  expect(f.editor._message.label.textContent).toContain('身份已改变');
});

test('切换 input 立即释放未完成解码的 URL，迟到完成只保留新图', async () => {
  const f = fixture();
  const oldLoad = f.load();
  await flush();
  const nextLoad = f.load(new Input(f.input.resource.with({ path: '/workspace/root/next.png' })));
  await flush();
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1']]);
  f.decodes[1]!.resolve();
  await nextLoad;
  f.decodes[0]!.resolve();
  await oldLoad;
  expect(f.viewer._image.src).toBe('blob:image-2');
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1']]);
});

test('watcher 删除读取失败撤下旧图，后续恢复仍沿原 watcher 重读', async () => {
  const f = fixture();
  const loading = f.load();
  await flush();
  f.decodes[0]!.resolve();
  await loading;
  f.read.mockRejectedValueOnce(new Error('文件已删除'));
  f.changes.fire({ contains: () => true });
  await new Promise(resolve => setTimeout(resolve, 130));
  expect(f.viewer._image.src).toBe('');
  expect(f.editor._message.label.textContent).toContain('文件已删除');
  expect(f.editor.fileSize).toBeUndefined();
  expect(f.revokeUrl.mock.calls).toEqual([['blob:image-1']]);
  expect(f.watches()).toBe(1);
  f.changes.fire({ contains: () => true });
  await new Promise(resolve => setTimeout(resolve, 130));
  f.decodes[1]!.resolve();
  await flush();
  expect(f.viewer._image.src).toBe('blob:image-2');
  expect(f.editor._message.label.textContent).toBe('');
  expect(f.editor.fileSize).toBe(1);
});
