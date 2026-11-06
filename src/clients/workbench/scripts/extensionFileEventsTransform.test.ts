import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { DisposableMap, DisposableStore, toDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { raceCancellation } from '../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { FileOperation } from '../upstream/src/vs/platform/files/common/files.js';
import { ProgressLocation } from '../upstream/src/vs/platform/progress/common/progress.js';
import Severity from '../upstream/src/vs/base/common/severity.js';
import { NotificationTelemetryId } from '../upstream/src/vs/platform/notification/common/notificationTelemetry.js';
import { StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { TargetUriTransformer, assertHostWorkspaceEdit, hostOwnsFileEventResource } from '../src/extensions/targetUris.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);

test('目标宿主只接受自身 view 与具名配置事件，配置父目录只用于 watch，Web 保留原全局范围', () => {
  const { host, transformer } = makeHost('workspace-a', 'gateway-a');
  const workspace = URI.joinPath(transformer.folders[0].uri, 'src/main.ts');
  const otherView = URI.parse('boxteam-file://gateway-a/workspace-b/root/src/main.ts');
  const settings = URI.parse('boxteam://gateway-a/profile/default/settings');
  const sibling = URI.parse('boxteam://gateway-a/profile/default/extensions.json');
  const settingsDirectory = URI.parse('boxteam://gateway-a/profile/default');

  expect(hostOwnsFileEventResource(host, workspace)).toBe(true);
  expect(hostOwnsFileEventResource(host, otherView)).toBe(false);
  expect(hostOwnsFileEventResource(host, settings)).toBe(true);
  expect(hostOwnsFileEventResource(host, sibling)).toBe(false);
  expect(hostOwnsFileEventResource(host, settingsDirectory)).toBe(false);
  expect(hostOwnsFileEventResource(host, settingsDirectory, true)).toBe(true);
  expect(hostOwnsFileEventResource({ binding: undefined } as never, otherView)).toBe(true);
});

test('编译执行原文件事件 owner：按宿主隔离广播、拒绝跨 view watch，dispose 等待弹窗后不会 apply', async () => {
  const path = join(root, 'upstream/src/vs/workbench/api/browser/mainThreadFileSystemEventService.ts');
  const converted = await transform(path, await readFile(path, 'utf8'));
  const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(ts.isClassDeclaration);
  if (!owner) throw new Error('转换后缺少 MainThreadFileSystemEventService');
  let code = owner.getText(parsed).replace(/^@extHostNamedCustomer\([^\n]*\)\s*/, '').replace(/^export class /, 'class ').replace(/^\s*@\w+\s+/gm, '');
  const javascript = ts.transpile(code, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  const MainThread = new Function('DisposableMap', 'DisposableStore', 'CancellationTokenSource', 'raceCancellation',
    'FileOperation', 'ProgressLocation', 'NotificationTelemetryId', 'Severity', 'URI', 'hostOwnsFileEventResource', 'assertHostWorkspaceEdit',
    'ExtHostContext', 'StorageScope', 'StorageTarget', 'localize', 'reviveWorkspaceEditDto', `${javascript}\nreturn MainThreadFileSystemEventService;`)(
    DisposableMap, DisposableStore, CancellationTokenSource, raceCancellation, FileOperation, ProgressLocation,
    NotificationTelemetryId, Severity, URI, hostOwnsFileEventResource, assertHostWorkspaceEdit, { ExtHostFileSystemEventService: {} }, StorageScope, StorageTarget,
    (key: string, ...args: unknown[]) => args.length ? String(args[0]) : key,
    (edit: unknown) => edit,
  ) as new (...args: unknown[]) => {
    dispose(): void;
    $watch(extensionId: string, session: number, resource: object, opts: object, correlate: boolean): Promise<void>;
  };
  type Captured = { listener: (event: { rawAdded: URI[]; rawUpdated: URI[]; rawDeleted: URI[] }) => void;
    participant: (files: { source?: URI; target: URI }[], operation: number, undo: undefined, timeout: number, token: object) => Promise<void> };
  const create = (host: object, proxy: { $onFileEvent(event: object): void; $onWillRunFileOperation(): Promise<unknown> }, prompt: () => Promise<{ result: number; checkboxChecked: boolean }>) => {
    const captured: Captured = { listener: () => undefined, participant: async () => undefined };
    let watcherCalls = 0;
    const files = {
      onDidFilesChange(listener: Captured['listener']) { captured.listener = listener; return toDisposable(() => undefined); },
      canHandleResource: async () => { watcherCalls++; return true; },
      stat: async () => ({ isDirectory: true }), watch: () => toDisposable(() => undefined),
      createWatcher: () => ({ onDidChange: () => toDisposable(() => undefined) }),
    };
    const work = {
      addFileOperationParticipant(value: { participate: Captured['participant'] }) { captured.participant = value.participate.bind(value); return toDisposable(() => undefined); },
      onDidRunWorkingCopyFileOperation: () => toDisposable(() => undefined),
    };
    const service = new MainThread({ getProxy: () => proxy, extensionHost: host }, files,
      work, { apply: async () => { applied++; } },
      { withProgress: (_options: object, task: () => Promise<unknown>) => task() },
      { confirm: async () => ({ confirmed: true }), prompt },
      { getBoolean: () => undefined, store: () => undefined }, { info: () => undefined }, {}, {}, { info: () => undefined, warn: () => undefined, trace: () => undefined });
    return { service, captured, watcherCalls: () => watcherCalls };
  };
  let applied = 0;
  const { host: hostA, transformer: transformerA } = makeHost('workspace-a', 'gateway-a');
  const { host: hostB, transformer: transformerB } = makeHost('workspace-b', 'gateway-b');
  const uriA = URI.joinPath(transformerA.folders[0].uri, 'src/main.ts');
  const uriB = URI.joinPath(transformerB.folders[0].uri, 'src/main.ts');
  const deliveredA: object[] = [], deliveredB: object[] = [];
  const promptA = Promise.resolve({ result: 0, checkboxChecked: false });
  const a = create(hostA, { $onFileEvent: event => deliveredA.push(event), $onWillRunFileOperation: async () => undefined }, () => promptA);
  const b = create(hostB, { $onFileEvent: event => deliveredB.push(event), $onWillRunFileOperation: async () => undefined }, async () => ({ result: 0, checkboxChecked: false }));

  a.captured.listener({ rawAdded: [uriA, uriB], rawUpdated: [], rawDeleted: [] });
  b.captured.listener({ rawAdded: [uriA, uriB], rawUpdated: [], rawDeleted: [] });
  expect((deliveredA[0] as { created: object[] }).created.map(value => URI.revive(value as never).toString())).toEqual([uriA.toString()]);
  expect((deliveredB[0] as { created: object[] }).created.map(value => URI.revive(value as never).toString())).toEqual([uriB.toString()]);
  await expect(a.service.$watch('sample.extension', 1, uriB.toJSON(), {}, false)).rejects.toThrow('超出发起宿主工作区视图');
  expect(a.watcherCalls()).toBe(0);

  let finishPrompt!: (value: { result: number; checkboxChecked: boolean }) => void;
  let promptEntered = false;
  const promptWait = new Promise<{ result: number; checkboxChecked: boolean }>(resolve => { finishPrompt = resolve; });
  const waiting = create(hostA, { $onFileEvent: () => undefined, $onWillRunFileOperation: async () => ({
    edit: { edits: [{ resource: uriA.toJSON() }] }, extensionNames: ['sample.extension'],
  }) }, () => { promptEntered = true; return promptWait; });
  const participation = waiting.captured.participant([{ target: uriA }], FileOperation.CREATE, undefined, 5_000, CancellationToken.None);
  for (let attempt = 0; attempt < 10 && !promptEntered; attempt++) await Promise.resolve();
  expect(promptEntered).toBe(true);
  waiting.service.dispose();
  finishPrompt({ result: 0, checkboxChecked: false });
  await expect(participation).rejects.toThrow('宿主代际已经关闭');
  expect(applied).toBe(0);

  a.service.dispose(); b.service.dispose();
});

test('普通与 Notebook save participant 都拒绝向其它宿主调用保存回调', async () => {
  const cases = [
    ['src/vs/workbench/api/browser/mainThreadSaveParticipant.ts', 'editorModel.resource'],
    ['src/vs/workbench/api/browser/mainThreadNotebookSaveParticipant.ts', 'workingCopy.resource'],
  ] as const;
  for (const [relativePath, resourceName] of cases) {
    const path = join(root, 'upstream', relativePath);
    const converted = await transform(path, await readFile(path, 'utf8'));
    const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
    const participant = parsed.statements.find(ts.isClassDeclaration);
    if (!participant) throw new Error(`转换后缺少 save participant: ${relativePath}`);
    const code = participant.getText(parsed).replace(/^export class /, 'class ');
    const javascript = ts.transpile(code, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
    let calls = 0;
    const TestClass = new Function('hostOwnsFileEventResource', 'extHostCustomer', 'ExtHostContext',
      'localize', 'raceCancellationError', 'shouldSynchronizeModel', 'NotebookFileWorkingCopyModel',
      `${javascript}\nreturn ${participant.name?.text};`)(
      hostOwnsFileEventResource, () => undefined, { ExtHostDocumentSaveParticipant: {}, ExtHostNotebookDocumentSaveParticipant: {} },
      (key: string) => key, async (promise: Promise<void>) => promise, () => true, class NotebookFileWorkingCopyModel {}) as new (context: object) => {
        participate(model: Record<string, unknown>, context: object, progress: object, token: CancellationToken): Promise<void>;
      };
    const { host } = makeHost('workspace-a', 'gateway-a');
    const participantInstance = new TestClass({ extensionHost: host, getProxy: () => ({ $participateInSave: async () => { calls++; return [true]; } }) });
    const external = URI.parse('boxteam-file://gateway-b/workspace-b/boxteam%3A%2F%2Fworkspace%2Fworkspace-b%2Fresources%2Ffiles%2Froot/src/main.ts');
    const model = resourceName === 'editorModel.resource' ? { resource: external, textEditorModel: {} } : { resource: external, model: {} };
    await participantInstance.participate(model, { reason: 1 }, {}, CancellationToken.None);
    expect(calls).toBe(0);
  }
});

let applied = 0;
function makeHost(workspaceId: string, gatewayId: string) {
  const nativeRoot = `/target/${workspaceId}`;
  const canonicalRoot = URI.from({ scheme: 'boxteam', authority: 'workspace', path: `/${workspaceId}/resources/files/root` }).toString();
  const binding = {
    gateway_id: gatewayId, backend_id: `00000000-0000-4000-8000-${workspaceId === 'workspace-a' ? '000000000001' : '000000000002'}`,
    initialization: { os: 'linux', parent_pid: 1, logs_location_uri: 'file:///target/logs',
      environment: { app_root_uri: 'file:///target/app', global_storage_home_uri: 'file:///target/global',
        workspace_storage_home_uri: 'file:///target/storage', profile_id: 'default',
        remote_settings: { native_uri: 'file:///target/profile/settings.json', resource_uri: `boxteam://${gatewayId}/profile/default/settings` } },
      workspace: { id: 'view-a', name: 'View A', folders: [{ workspace_id: workspaceId, name: 'A', resource_uri: canonicalRoot, native_uri: `file://${nativeRoot}` }],
        configuration: { native_uri: `file://${nativeRoot}.code-workspace`, resource_uri: `boxteam://${gatewayId}/${workspaceId}/configuration` } } },
  };
  const transformer = new TargetUriTransformer(binding as never, () => []);
  return { transformer, host: { binding, uriTransformer: transformer } as never };
}
