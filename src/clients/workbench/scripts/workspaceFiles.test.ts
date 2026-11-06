import { describe, expect, test, spyOn } from 'bun:test';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { joinPath, relativePath, isEqualOrParent } from '../upstream/src/vs/base/common/resources.js';
import { FileService } from '../upstream/src/vs/platform/files/common/fileService.js';
import { FileType, FileChangeType, FileSystemProviderErrorCode, toFileSystemProviderErrorCode, type IFileChange } from '../upstream/src/vs/platform/files/common/files.js';
import { VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import { NullLogService } from '../upstream/src/vs/platform/log/common/log.js';
import { UriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentityService.js';
import { gateway, GatewayRequestError } from '../src/agentApi.js';
import { installBackendFiles, workspaceFileUri, workspaceFileSource } from '../src/workspaceFiles.js';
import * as fileEvents from '../../web/src/api/stream/workspaceFileEvents.js';
import * as fileApi from '../../web/src/api/workspaceFilesystem.js';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { consumeStream } from '../upstream/src/vs/base/common/stream.js';
import { createHash } from 'node:crypto';
import '../../web/node_modules/fake-indexeddb/auto/index.js';
import { openWorkspaceFileIntentStore } from '../../web/src/state/workspaceFiles/intentStore.js';
import { dispatchWorkspaceFileIntent, waitForWorkspaceFileOperation } from '../../web/src/state/workspaceFiles/dispatcher.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import type { WorkspaceFileOperationReceipt } from '../../web/src/types/backend.js';

const owner = { gatewayId: 'gateway_file_test', workspaceId: 'workspace-a' };
const ownerKey = gatewayWorkspaceKey(owner);
const root = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;

// 通过原版 FileService 验证实际 provider 调用，不只测试投影函数。
function fileService() {
  const files = new FileService(new NullLogService());
  installBackendFiles(files, new UriIdentityService(files));
  return files;
}

describe('后端文件目录根和稳定投影', () => {
  test('根拼接／相对路径和序列化往返保留 Unicode 与字面百分号', () => {
    const directory = workspaceFileUri(owner, root, root);
    for (const name of ['中文 目录/🧪 %.txt', '%2F.txt', '含#?@.txt']) {
      const address = root + '/' + name.split('/').map(encodeURIComponent).join('/');
      const file = workspaceFileUri(owner, address, root);
      expect(joinPath(directory, name).toString()).toBe(file.toString());
      expect(relativePath(directory, file)).toBe(name);
      expect(isEqualOrParent(file, directory)).toBe(true);
      expect(workspaceFileSource(URI.parse(file.toString()))).toEqual({ ...owner, path: address, root });
      expect(file.query).toBe('');
    }
    const punctuation = joinPath(directory, "a!'()*.txt");
    expect(workspaceFileSource(punctuation).path).toBe(root + '/a%21%27%28%29%2A.txt');
    const otherRoot = root.replace('/files/root', '/files/other');
    expect(isEqualOrParent(workspaceFileUri(owner, otherRoot + '/x.txt', otherRoot), directory)).toBe(false);
    expect(workspaceFileUri({ ...owner, gatewayId: 'another-gateway' }, root, root).toString()).not.toBe(directory.toString());
    expect(() => workspaceFileSource(URI.from({ scheme: 'boxteam-file', authority: 'workspace-a', path: '/x.txt', query: 'route=old&path=old' }))).toThrow('旧临时路由');
    const media = workspaceFileUri(owner, root + '/video.mp4', root);
    expect(workspaceFileSource(media.with({ query: 'version=123' })).path).toBe(root + '/video.mp4');
    expect(() => workspaceFileSource(media.with({ query: 'revision=123' }))).toThrow();
    expect(() => workspaceFileSource(media.with({ fragment: 'L2' }))).toThrow();
  });

  test('目录和二进制 stat 只 describe，缺失和权限错误由原服务返回', async () => {
    const files = fileService();
    const discovery = spyOn(gateway, 'workspaces').mockRejectedValue(new Error('文件I/O不应枚举工作区'));
    const request = spyOn(gateway, 'request');
    try {
      for (const [path, shape, expectedType] of [[root, 'directory', FileType.Directory], [root + '/image.bin', 'file', FileType.File],
        [root.replace(`${owner.gatewayId}/`, ''), 'directory', FileType.Directory],
        [root.replace(`${owner.gatewayId}/`, '') + '/image.bin', 'file', FileType.File]] as const) {
        const sourceRoot = path.startsWith(root) ? root : root.replace(`${owner.gatewayId}/`, '');
        const returnedUri = path.startsWith(root) ? path : path.replace('boxteam://', `boxteam://${owner.gatewayId}/`);
        request.mockResolvedValueOnce({ node: { uri: returnedUri, resource_shape: shape,
          stat: { size: 73, modified_at_ms: 1200, changed_at_ms: 1100 } } });
        const stat = await files.stat(workspaceFileUri(owner, path, sourceRoot));
        expect(stat).toMatchObject({ isDirectory: expectedType === FileType.Directory, isFile: expectedType === FileType.File, size: 73, mtime: 1200, ctime: 1100 });
        const [endpoint, options, routeId] = request.mock.calls.at(-1)!;
        expect(endpoint).toBe(`/api/v1/workspaces/${owner.workspaceId}/resources/navigation`);
        expect(options?.method).toBe('POST');
        expect(JSON.parse(String(options?.body))).toEqual({ uri: path, operation: 'describe' });
        expect(routeId).toBe(ownerKey);
      }
      request.mockRejectedValueOnce(new GatewayRequestError(404, 'Not Found', '目录不存在', '/resources/navigation', 'missing-request'));
      await expect(files.stat(workspaceFileUri(owner, root + '/missing', root))).rejects.toThrow('目录不存在');
      request.mockRejectedValueOnce(new GatewayRequestError(403, 'Forbidden', '没有访问权限', '/resources/navigation', 'denied-request'));
      await expect(files.stat(workspaceFileUri(owner, root + '/denied', root))).rejects.toThrow('没有访问权限');
      expect(discovery).not.toHaveBeenCalled();
    } finally { discovery.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  for (const concurrentWrite of [false, true]) test(`首次文件写入复用 create intent 与空文件 CAS，创建后并发修改=${concurrentWrite}`, async () => {
    const files = fileService();
    const path = root + '/.vscode/settings.json';
    const resource = workspaceFileUri(owner, path, root);
    const workspaceKey = gatewayWorkspaceKey(owner);
    const store = await openWorkspaceFileIntentStore();
    const request = spyOn(gateway, 'request');
    const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root, filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
    const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob').mockImplementation(async (_port, requestPath) => {
      if (requestPath === path) throw new GatewayRequestError(404, 'Not Found', 'missing', '/raw', 'missing');
      return new Blob();
    });
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    for (const stale of await store.load(scope)) await store.remove(stale);
    const previous = await store.insert({ scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace', root, path: root }, directoryRevision: 1,
      command: { kind: 'create', name: 'previous.txt', entryKind: 'file' } });
    let sequence = 0;
    let currentContent = '';
    const revision = () => createHash('sha256').update(currentContent).digest('hex');
    const created = new Set<string>();
    const order: string[] = [];
    const accepted = new Map<string, WorkspaceFileOperationReceipt>();
    accepted.set(previous.intent.client_operation_id, { operation_id: previous.intent.client_operation_id,
      client_sequence: previous.intent.client_sequence, queue_seq: 1, kind: 'create', state: 'committed', receipt_revision: 2,
      effect: 'complete', items: [], directories: [], snapshots: [], created_path: root + '/previous.txt' });
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async (_port, operationIds) => {
      const receipt = accepted.get(operationIds[0]);
      if (!receipt) throw new GatewayRequestError(404, 'Not Found', 'missing operation', '/operations', 'missing');
      order.push(`status:${operationIds[0]}`);
      return { operations: [receipt], worker_state: 'running' };
    });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockImplementation(async (_port, directoryPath, payload) => {
      order.push(`create:${directoryPath}:${payload.name}`);
      const receipt: WorkspaceFileOperationReceipt = { operation_id: payload.intent.client_operation_id,
        client_sequence: payload.intent.client_sequence, queue_seq: ++sequence, kind: 'create', state: 'queued', receipt_revision: 1,
        effect: 'none', items: [], directories: [], snapshots: [], created_path: `${directoryPath}/${payload.name}` };
      accepted.set(receipt.operation_id, { ...receipt, state: 'committed', receipt_revision: 2, effect: 'complete' });
      created.add(receipt.created_path!);
      // 模拟空文件已经发布，但本次首次正文尚未提交时，另一写入方完成修改。
      if (payload.kind === 'file' && concurrentWrite) currentContent = 'external writer';
      return receipt;
    });
    try {
      request.mockImplementation(async (endpoint, options) => {
        if (endpoint === '/api/v1/workspaces/workspace-a/resources/navigation') {
          const uri = JSON.parse(String(options?.body)).uri as string;
          if (uri === root || created.has(uri)) return { node: { uri, resource_shape: uri === path ? 'file' : 'directory', stat: { size: currentContent.length, modified_at_ms: 1, changed_at_ms: 1 } } };
          throw new GatewayRequestError(404, 'Not Found', 'missing', endpoint, 'missing');
        }
        if (endpoint.startsWith('/api/v1/workspace/files?')) {
          const directory = new URLSearchParams(endpoint.split('?')[1]).get('path')!;
          order.push(`list:${directory}`);
          return { path: directory, directory_key: { workspace_id: owner.workspaceId, scope: 'workspace', root, path: directory },
            directory_revision: 2, items: [], root_path: root, truncated: false, limit: 500 };
        }
        if (endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method === 'PUT') {
          const update = JSON.parse(String(options.body));
          if (update.expected_revision !== revision()) throw new GatewayRequestError(409, 'Conflict', '文件已被其他写入方修改', endpoint, 'concurrent-write');
          currentContent = update.content;
          return { path, content: currentContent, revision: revision() };
        }
        if (endpoint.startsWith('/api/v1/workspace/files/content?')) return { path, content: currentContent, revision: revision() };
        throw new Error(`unexpected request ${endpoint}`);
      });
      if (concurrentWrite) await expect(files.createFile(resource, VSBuffer.fromString('{}'))).rejects.toThrow('后端文件已变化');
      else await files.createFile(resource, VSBuffer.fromString('{}'));
      expect(create.mock.calls.map(([, directoryPath, payload]) => [directoryPath, payload.name, payload.kind])).toEqual([
        [root, '.vscode', 'directory'], [root + '/.vscode', 'settings.json', 'file'],
      ]);
      expect(order.indexOf(`status:${previous.intent.client_operation_id}`)).toBeLessThan(order.indexOf(`list:${root}`));
      expect(create.mock.calls.map(([, , payload]) => payload.intent.client_sequence)).toEqual([
        previous.intent.client_sequence + 1, previous.intent.client_sequence + 2,
      ]);
      const put = request.mock.calls.find(([endpoint, options]) => endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method === 'PUT')!;
      expect(JSON.parse(String(put[1]?.body))).toEqual({ content: '{}', expected_revision: createHash('sha256').update('').digest('hex') });
      expect(currentContent).toBe(concurrentWrite ? 'external writer' : '{}');
      expect(request.mock.calls.filter(([endpoint, options]) => endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method !== 'PUT')).toHaveLength(concurrentWrite ? 1 : 0);
      expect(await store.load(scope)).toEqual([]);
      expect(roots.mock.calls[0]).toEqual([0, workspaceKey]);
    } finally { await store.remove(previous); create.mockRestore(); status.mockRestore(); raw.mockRestore(); roots.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  for (const rejection of ['http', 'queued'] as const) {
    for (const target of ['directory', 'file', 'missing', 'denied', ...(rejection === 'queued' ? ['other-receipt'] as const : [])] as const) {
      test(`并发写入共享缺失父目录，${rejection} 冲突核实目标=${target}`, async () => {
        const files = fileService();
        const parentName = `mkdirp-${rejection}-${target}`;
        const parent = `${root}/${parentName}`;
        const paths = [`${parent}/one.txt`, `${parent}/two.txt`];
        const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
        const store = await openWorkspaceFileIntentStore();
        for (const stale of await store.load(scope)) await store.remove(stale);
        const bothMissing = Promise.withResolvers<void>();
        const bothSnapshots = Promise.withResolvers<void>();
        const conflictFinished = Promise.withResolvers<void>();
        const accepted = new Map<string, WorkspaceFileOperationReceipt>();
        const content = new Map<string, string>();
        const mkdirErrors: unknown[] = [];
        const verified: string[] = [];
        let missingParents = 0, parentSnapshots = 0, parentCreates = 0, sequence = 0;
        let conflictPending = false;
        let rejectedId = '';
        const sendError = new GatewayRequestError(409, 'Conflict', '共享父目录创建冲突', '/entries', 'mkdirp-conflict');
        const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root,
          filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
        const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob').mockRejectedValue(new Error('新文件不应重读正文'));
        const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async (port, ids, route, backendId) => {
          expect([port, route, backendId]).toEqual([0, ownerKey, owner.workspaceId]);
          expect(ids).toHaveLength(1);
          const receipt = accepted.get(ids[0]);
          if (!receipt) throw new GatewayRequestError(404, 'Not Found', '原 operation 未受理', '/operations', 'mkdirp-operation-missing');
          return { operations: [receipt], worker_state: 'running' };
        });
        const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockImplementation(async (port, directoryPath, payload, route) => {
          expect([port, route]).toEqual([0, ownerKey]);
          expect(payload.intent.directory_key).toEqual({ workspace_id: owner.workspaceId, scope: 'workspace', root, path: directoryPath });
          const receipt: WorkspaceFileOperationReceipt = { operation_id: payload.intent.client_operation_id,
            client_sequence: payload.intent.client_sequence, queue_seq: ++sequence, kind: 'create', state: 'committed', receipt_revision: 2,
            effect: 'complete', items: [], directories: [], snapshots: [], created_path: `${directoryPath}/${payload.name}` };
          if (payload.kind === 'directory') {
            expect([directoryPath, payload.name]).toEqual([root, parentName]);
            if (++parentCreates === 1) {
              accepted.set(receipt.operation_id, receipt);
              // 已提交的创建可由另一 waiter 查询；保留本次响应，确保冲突先在原 mkdirp 链路结清。
              await conflictFinished.promise;
              return receipt;
            }
            expect(parentCreates).toBe(2);
            rejectedId = receipt.operation_id;
            conflictPending = true;
            if (rejection === 'http') throw sendError;
            accepted.set(receipt.operation_id, { ...receipt, state: 'rejected', effect: 'none',
              created_path: target === 'other-receipt' ? `${root}/another-directory` : parent,
              error_code: 'file_operation_conflict', error_detail: '共享父目录 worker 拒绝' });
            return { ...receipt, state: 'queued', receipt_revision: 1, effect: 'none' };
          }
          expect(directoryPath).toBe(parent);
          expect(paths).toContain(receipt.created_path!);
          content.set(receipt.created_path!, '');
          accepted.set(receipt.operation_id, receipt);
          return receipt;
        });
        const request = spyOn(gateway, 'request').mockImplementation(async (endpoint, options, route) => {
          expect(route).toBe(ownerKey);
          if (endpoint === `/api/v1/workspaces/${owner.workspaceId}/resources/navigation`) {
            const { uri, operation } = JSON.parse(String(options?.body));
            expect(options?.method).toBe('POST');
            expect(operation).toBe('describe');
            expect([root, parent, ...paths]).toContain(uri);
            if (uri === parent && parentCreates === 0) {
              // 两个真实 writeFile 必须先看到相同父目录缺失，防止用例退化成串行写入。
              if (++missingParents === 2) bothMissing.resolve();
              await bothMissing.promise;
              throw new GatewayRequestError(404, 'Not Found', '父目录尚未创建', endpoint, 'mkdirp-parent-missing');
            }
            if (uri === parent && conflictPending) {
              verified.push(uri);
              conflictPending = false;
              // 核实瞬间目标可能被替换或删除；随后恢复目录也不能追认本次 mkdir 成功。
              if (target === 'missing') throw new GatewayRequestError(404, 'Not Found', '冲突目标已消失', endpoint, 'mkdirp-target-missing');
              if (target === 'denied') throw new GatewayRequestError(403, 'Forbidden', '冲突目标无权核实', endpoint, 'mkdirp-target-denied');
              return { node: { uri, resource_shape: target === 'file' ? 'file' : 'directory',
                stat: { size: 0, modified_at_ms: 1, changed_at_ms: 1 } } };
            }
            if (uri === root || uri === parent || content.has(uri)) return { node: { uri,
              resource_shape: content.has(uri) ? 'file' : 'directory',
              stat: { size: content.get(uri)?.length ?? 0, modified_at_ms: 1, changed_at_ms: 1 } } };
            throw new GatewayRequestError(404, 'Not Found', '新文件尚未创建', endpoint, 'mkdirp-file-missing');
          }
          if (endpoint.startsWith('/api/v1/workspace/files?')) {
            const directory = new URLSearchParams(endpoint.split('?')[1]).get('path')!;
            expect([root, parent]).toContain(directory);
            if (directory === root) {
              // 两个 createEntry 都完成前序读取后才插入意图，覆盖真实并发的同目录依赖。
              if (++parentSnapshots === 2) bothSnapshots.resolve();
              await bothSnapshots.promise;
            }
            return { path: directory, directory_key: { workspace_id: owner.workspaceId, scope: 'workspace', root, path: directory },
              directory_revision: 1, items: [], root_path: root, truncated: false, limit: 500 };
          }
          if (endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method === 'PUT') {
            const path = new URLSearchParams(endpoint.split('?')[1]).get('path')!;
            const update = JSON.parse(String(options.body));
            expect(paths).toContain(path);
            expect(content.get(path)).toBe('');
            expect(update.expected_revision).toBe(createHash('sha256').update('').digest('hex'));
            content.set(path, update.content);
            return { path, content: update.content, revision: createHash('sha256').update(update.content).digest('hex') };
          }
          throw new Error(`unexpected request ${endpoint}`);
        });
        const provider = files.getProvider(workspaceFileUri(owner, parent, root).scheme)!;
        const originalMkdir = provider.mkdir.bind(provider);
        const mkdir = spyOn(provider, 'mkdir').mockImplementation(async resource => {
          try { await originalMkdir(resource); }
          catch (error) { mkdirErrors.push(error); throw error; }
          finally { conflictFinished.resolve(); }
        });
        try {
          const results = await Promise.allSettled(paths.map((path, index) =>
            files.writeFile(workspaceFileUri(owner, path, root), VSBuffer.fromString(`body-${index}`))));
          expect(missingParents).toBe(2);
          expect(parentSnapshots).toBe(2);
          expect(mkdir.mock.calls.map(([resource]) => workspaceFileSource(resource).path)).toEqual([parent, parent]);
          expect(create.mock.calls.filter(([, , payload]) => payload.kind === 'directory')).toHaveLength(2);
          expect(verified).toEqual(target === 'other-receipt' ? [] : [parent]);
          expect(mkdirErrors).toHaveLength(1);
          const error = mkdirErrors[0];
          expect(toFileSystemProviderErrorCode(error)).toBe(target === 'directory'
            ? FileSystemProviderErrorCode.FileExists : FileSystemProviderErrorCode.Unknown);
          const failures = results.filter(result => result.status === 'rejected');
          expect(failures).toHaveLength(target === 'directory' ? 0 : 1);
          if (target !== 'directory') {
            expect((failures[0] as PromiseRejectedResult).reason.message).toContain(target === 'denied'
              ? '目录创建冲突后无法核实准确目标' : rejection === 'http' ? '文件操作未受理' : '共享父目录 worker 拒绝');
          }
          if (target === 'denied') {
            expect(error).toBeInstanceOf(AggregateError);
            expect((error as AggregateError).errors).toHaveLength(2);
            expect((error as AggregateError).errors[0].message).toContain(rejection === 'http' ? '文件操作未受理' : '共享父目录 worker 拒绝');
            expect((error as AggregateError).errors[1].message).toContain('冲突目标无权核实');
          }
          for (const [index, result] of results.entries()) expect(content.get(paths[index])).toBe(result.status === 'fulfilled' ? `body-${index}` : undefined);
          expect(raw).not.toHaveBeenCalled();
          expect(await store.load(scope)).toEqual([]);
          expect(create.mock.calls.filter(([, , payload]) => payload.intent.client_operation_id === rejectedId)).toHaveLength(1);
          expect(status.mock.calls.filter(([, ids]) => ids[0] === rejectedId)).toHaveLength(2);
        } finally {
          for (const stale of await store.load(scope)) await store.remove(stale);
          mkdir.mockRestore(); request.mockRestore(); create.mockRestore(); status.mockRestore(); raw.mockRestore(); roots.mockRestore(); files.dispose();
        }
      });
    }
  }

  test('恢复已终态后继先结清自身，不查询已移除失败前序或重发，后续真实创建可继续', async () => {
    const files = fileService();
    const store = await openWorkspaceFileIntentStore();
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    for (const stale of await store.load(scope)) await store.remove(stale);
    const input = { scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace' as const, root, path: root }, directoryRevision: 1 };
    const previous = await store.insert({ ...input, command: { kind: 'create', name: 'failed-parent', entryKind: 'directory' } });
    const successor = await store.insert({ ...input, command: { kind: 'create', name: 'failed-child.txt', entryKind: 'file' } });
    expect(successor.intent.depends_on).toEqual([previous.intent.client_operation_id]);
    const queued: WorkspaceFileOperationReceipt = { operation_id: successor.intent.client_operation_id,
      client_sequence: successor.intent.client_sequence, queue_seq: 2, kind: 'create', state: 'queued', receipt_revision: 1,
      effect: 'none', items: [], directories: [], snapshots: [], created_path: `${root}/failed-child.txt` };
    await store.accept(successor, queued);
    await store.remove(previous);
    const accepted = new Map<string, WorkspaceFileOperationReceipt>([
      [previous.intent.client_operation_id, { ...queued, operation_id: previous.intent.client_operation_id,
        client_sequence: previous.intent.client_sequence, queue_seq: 1, state: 'rejected', receipt_revision: 2,
        error_code: 'file_operation_conflict', error_detail: 'P 已失败' }],
      [successor.intent.client_operation_id, { ...queued, state: 'dependency_failed', receipt_revision: 2,
        error_code: 'file_operation_dependency_failed', error_detail: 'Q 后台依赖失败已结清' }],
    ]);
    const path = `${root}/after-recovery.txt`;
    let content: string | undefined;
    const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root,
      filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async (port, ids, route, backendId) => {
      expect([port, route, backendId]).toEqual([0, ownerKey, owner.workspaceId]);
      const receipt = accepted.get(ids[0]);
      if (!receipt) throw new GatewayRequestError(404, 'Not Found', 'operation 尚未受理', '/operations', 'recovery-operation-missing');
      return { operations: [receipt], worker_state: 'running' };
    });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockImplementation(async (port, directory, payload, route) => {
      expect([port, directory, route]).toEqual([0, root, ownerKey]);
      expect([payload.name, payload.kind]).toEqual(['after-recovery.txt', 'file']);
      expect(payload.intent.depends_on).toEqual([]);
      expect(payload.intent.client_sequence).toBe(successor.intent.client_sequence + 1);
      const receipt: WorkspaceFileOperationReceipt = { ...queued, operation_id: payload.intent.client_operation_id,
        client_sequence: payload.intent.client_sequence, queue_seq: 3, state: 'committed', receipt_revision: 2,
        effect: 'complete', created_path: path };
      accepted.set(receipt.operation_id, receipt);
      content = '';
      return receipt;
    });
    const request = spyOn(gateway, 'request').mockImplementation(async (endpoint, options, route) => {
      expect(route).toBe(ownerKey);
      if (endpoint === `/api/v1/workspaces/${owner.workspaceId}/resources/navigation`) {
        const { uri } = JSON.parse(String(options?.body));
        expect([root, path]).toContain(uri);
        if (uri === root || content !== undefined) return { node: { uri, resource_shape: uri === root ? 'directory' : 'file',
          stat: { size: content?.length ?? 0, modified_at_ms: 1, changed_at_ms: 1 } } };
        throw new GatewayRequestError(404, 'Not Found', '文件尚未创建', endpoint, 'recovery-file-missing');
      }
      if (endpoint.startsWith('/api/v1/workspace/files?')) return { path: root, directory_key: input.directoryKey,
        directory_revision: 1, items: [], root_path: root, truncated: false, limit: 500 };
      if (endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method === 'PUT') {
        const update = JSON.parse(String(options.body));
        expect(update.expected_revision).toBe(createHash('sha256').update('').digest('hex'));
        content = update.content;
        return { path, content, revision: createHash('sha256').update(update.content).digest('hex') };
      }
      throw new Error(`unexpected request ${endpoint}`);
    });
    try {
      const resource = workspaceFileUri(owner, path, root);
      await expect(files.writeFile(resource, VSBuffer.fromString('recovered'))).rejects.toThrow('前序文件操作未提交: Q 后台依赖失败已结清');
      expect(status.mock.calls.map(([, ids]) => ids)).toEqual([[successor.intent.client_operation_id]]);
      expect(create).not.toHaveBeenCalled();
      expect(await store.load(scope)).toEqual([]);
      await files.writeFile(resource, VSBuffer.fromString('recovered'));
      expect(content).toBe('recovered');
      expect(create).toHaveBeenCalledTimes(1);
      expect(status.mock.calls.map(([, ids]) => ids)).toEqual([
        [successor.intent.client_operation_id], [create.mock.calls[0][2].intent.client_operation_id],
      ]);
      expect(await store.load(scope)).toEqual([]);
    } finally {
      for (const stale of await store.load(scope)) await store.remove(stale);
      request.mockRestore(); create.mockRestore(); status.mockRestore(); roots.mockRestore(); files.dispose();
    }
  });

  for (const dependencyCommits of [false, true]) test(`未受理后继按原依赖等待后发送，自身不重复查询，前序提交=${dependencyCommits}`, async () => {
    const store = await openWorkspaceFileIntentStore();
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    for (const stale of await store.load(scope)) await store.remove(stale);
    const input = { scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace' as const, root, path: root }, directoryRevision: 1 };
    const previous = await store.insert({ ...input, command: { kind: 'create', name: 'previous-wait.txt', entryKind: 'file' } });
    const record = await store.insert({ ...input, command: { kind: 'create', name: 'successor-wait.txt', entryKind: 'file' } });
    const order: string[] = [];
    let previousQueries = 0;
    const receipt: WorkspaceFileOperationReceipt = { operation_id: previous.intent.client_operation_id,
      client_sequence: previous.intent.client_sequence, queue_seq: 1, kind: 'create', state: 'rejected', receipt_revision: 2,
      effect: 'none', items: [], directories: [], snapshots: [], error_detail: '原前序已失败' };
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async (port, ids, route, backendId) => {
      expect([port, route, backendId]).toEqual([0, ownerKey, owner.workspaceId]);
      order.push(`status:${ids[0]}`);
      if (ids[0] === record.intent.client_operation_id) throw new GatewayRequestError(404, 'Not Found', '后继尚未受理', '/operations', 'unaccepted-successor');
      expect(ids).toEqual([previous.intent.client_operation_id]);
      ++previousQueries;
      return { operations: [dependencyCommits ? { ...receipt, state: previousQueries === 1 ? 'running' : 'committed',
        receipt_revision: previousQueries, effect: previousQueries === 1 ? 'none' : 'complete' } : receipt], worker_state: 'running' };
    });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockImplementation(async (port, directory, payload, route) => {
      expect([port, directory, route]).toEqual([0, root, ownerKey]);
      expect(payload.intent).toEqual(record.intent);
      expect(previousQueries).toBe(2);
      order.push(`create:${payload.intent.client_operation_id}`);
      return { ...receipt, operation_id: record.intent.client_operation_id, client_sequence: record.intent.client_sequence,
        state: 'committed', effect: 'complete', created_path: `${root}/successor-wait.txt` };
    });
    try {
      const waiting = waitForWorkspaceFileOperation(0, record, store);
      if (dependencyCommits) expect((await waiting).state).toBe('committed');
      else await expect(waiting).rejects.toThrow('前序文件操作未提交，后继保持等待: 原前序已失败');
      expect(order).toEqual([`status:${record.intent.client_operation_id}`, `status:${previous.intent.client_operation_id}`,
        ...(dependencyCommits ? [`status:${previous.intent.client_operation_id}`, `create:${record.intent.client_operation_id}`] : [])]);
      expect(create).toHaveBeenCalledTimes(dependencyCommits ? 1 : 0);
      expect((await store.load(scope)).find(value => value.intent.client_operation_id === record.intent.client_operation_id)?.receipt?.state)
        .toBe(dependencyCommits ? 'committed' : undefined);
    } finally {
      for (const stale of await store.load(scope)) await store.remove(stale);
      create.mockRestore(); status.mockRestore();
    }
  });

  test('文件树受理入口返回 queued，不进入终态轮询或等待其依赖', async () => {
    const store = await openWorkspaceFileIntentStore();
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    const input = { scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace' as const, root, path: root }, directoryRevision: 1 };
    const previous = await store.insert({ ...input, command: { kind: 'create', name: 'accepted-parent', entryKind: 'directory' } });
    const record = await store.insert({ ...input, command: { kind: 'create', name: 'accepted-child.txt', entryKind: 'file' } });
    expect(record.intent.depends_on).toEqual([previous.intent.client_operation_id]);
    const queued: WorkspaceFileOperationReceipt = { operation_id: record.intent.client_operation_id,
      client_sequence: record.intent.client_sequence, queue_seq: 2, kind: 'create', state: 'queued', receipt_revision: 1,
      effect: 'none', items: [], directories: [], snapshots: [], created_path: `${root}/accepted-child.txt` };
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async (_port, ids) => {
      expect(ids).toEqual([record.intent.client_operation_id]);
      throw new GatewayRequestError(404, 'Not Found', '原 operation 未受理', '/operations', 'accept-only-missing');
    });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockResolvedValue(queued);
    try {
      expect(await dispatchWorkspaceFileIntent(0, record)).toEqual(queued);
      expect(status).toHaveBeenCalledTimes(1);
      expect(create.mock.calls).toEqual([[0, root, { name: 'accepted-child.txt', kind: 'file', intent: record.intent }, ownerKey]]);
      expect((await store.load(scope)).map(value => value.intent.client_operation_id)).toEqual([
        previous.intent.client_operation_id, record.intent.client_operation_id,
      ]);
    } finally { await store.remove(record); await store.remove(previous); create.mockRestore(); status.mockRestore(); }
  });

  test('已有文件尊重 create/overwrite 标志，覆盖仍使用原字节 revision', async () => {
    const files = fileService();
    const exists = workspaceFileUri(owner, root + '/existing.json', root);
    const provider = files.getProvider(exists.scheme)!;
    const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root, filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
    const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob');
    const request = spyOn(gateway, 'request').mockImplementation(async (endpoint, options) => {
      if (endpoint === '/api/v1/workspaces/workspace-a/resources/navigation') return { node: { uri: root + '/existing.json', resource_shape: 'file', stat: { size: 2, modified_at_ms: 1, changed_at_ms: 1 } } };
      if (endpoint.startsWith('/api/v1/workspace/files/content?') && options?.method === 'PUT') return { path: root + '/existing.json', content: 'x', revision: 'c'.repeat(64) };
      throw new Error(`unexpected request ${endpoint}`);
    });
    try {
      await expect(provider.writeFile(exists, new TextEncoder().encode('x'), { create: false, overwrite: false, unlock: false, atomic: false })).rejects.toThrow();
      expect(raw).not.toHaveBeenCalled();
      raw.mockResolvedValue(new Blob(['old']));
      await provider.writeFile(exists, new TextEncoder().encode('x'), { create: false, overwrite: true, unlock: false, atomic: false });
      expect(raw).toHaveBeenCalledTimes(1);
      const baseline = createHash('sha256').update('old').digest('hex');
      await expect(provider.writeFile(exists, new TextEncoder().encode('x'), { create: true, overwrite: false, unlock: false, atomic: false })).rejects.toThrow();
      expect(request.mock.calls).toHaveLength(4);
      const write = request.mock.calls.find(([, options]) => options?.method === 'PUT')!;
      expect(JSON.parse(String(write[1]?.body))).toEqual({ content: 'x', expected_revision: baseline });
      expect(request.mock.calls.filter(([endpoint]) => endpoint === '/api/v1/workspace/files/entries')).toHaveLength(0);
    } finally { raw.mockRestore(); roots.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  test('工作区切换取消只终止本地 waiter，未确认 intent 继续持久保留', async () => {
    const store = await openWorkspaceFileIntentStore();
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    const stale = await store.load(scope);
    for (const value of stale) await store.remove(value);
    const record = await store.insert({ scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace', root, path: root }, directoryRevision: 1,
      command: { kind: 'create', name: 'pending.txt', entryKind: 'file' } });
    const controller = new AbortController();
    const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root, filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry').mockResolvedValue({ operation_id: record.intent.client_operation_id,
      client_sequence: record.intent.client_sequence, queue_seq: 1, kind: 'create', state: 'queued', receipt_revision: 1,
      effect: 'none', items: [], directories: [], snapshots: [], created_path: root + '/pending.txt' });
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(async () => {
      if (status.mock.calls.length === 1) throw new GatewayRequestError(404, 'Not Found', 'missing operation', '/operations', 'missing');
      controller.abort();
      return { operations: [{ operation_id: record.intent.client_operation_id, client_sequence: record.intent.client_sequence,
        queue_seq: 1, kind: 'create', state: 'running', receipt_revision: 2, effect: 'none', items: [], directories: [], snapshots: [] }],
        worker_state: 'running' };
    });
    try {
      await expect(waitForWorkspaceFileOperation(0, record, store, undefined, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
      expect(await store.load(scope)).toHaveLength(1);
      expect((await store.load(scope))[0].intent.client_operation_id).toBe(record.intent.client_operation_id);
      expect(create).toHaveBeenCalledTimes(1);
      expect(status).toHaveBeenCalledTimes(2);
    } finally { await store.remove(record); status.mockRestore(); create.mockRestore(); roots.mockRestore(); }
  });

  test('status 等待期间切换工作区后放行 404 不会继续 POST，原 intent 仍保留', async () => {
    const store = await openWorkspaceFileIntentStore();
    const scope = { ...owner, workspaceRoot: root, actor: 'local' as const };
    const record = await store.insert({ scope, directoryPath: root,
      directoryKey: { workspace_id: owner.workspaceId, scope: 'workspace', root, path: root }, directoryRevision: 1,
      command: { kind: 'create', name: 'status-pending.txt', entryKind: 'file' } });
    const controller = new AbortController();
    const entered = Promise.withResolvers<void>();
    const release = Promise.withResolvers<Awaited<ReturnType<typeof fileApi.getWorkspaceFileOperations>>>();
    const roots = spyOn(fileApi, 'getWorkspaceFileRoots').mockResolvedValue({ workspace_uri: root, filesystem_uri: 'boxteam://gateway/gateway_file_test/resources/files/root', filesystem_label: '/' });
    const status = spyOn(fileApi, 'getWorkspaceFileOperations').mockImplementation(() => { entered.resolve(); return release.promise; });
    const create = spyOn(fileApi, 'createWorkspaceFileEntry');
    try {
      const waiting = waitForWorkspaceFileOperation(0, record, store, undefined, controller.signal);
      await entered.promise;
      controller.abort();
      release.reject(new GatewayRequestError(404, 'Not Found', 'missing operation', '/operations', 'missing'));
      await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
      expect(create).not.toHaveBeenCalled();
      expect((await store.load(scope)).some(value => value.intent.client_operation_id === record.intent.client_operation_id)).toBe(true);
    } finally { await store.remove(record); create.mockRestore(); status.mockRestore(); roots.mockRestore(); }
  });

  test('stat 不推进读入版本，保存仍以原内容 CAS 拒绝覆盖', async () => {
    const files = fileService();
    const path = root + '/x.txt';
    const resource = workspaceFileUri(owner, path, root);
    const provider = files.getProvider(resource.scheme);
    if (!provider?.readFile || !provider.writeFile) throw new Error('文件 provider 未登记');
    const original = createHash('sha256').update('original').digest('hex'), changed = '2'.repeat(64);
    const request = spyOn(gateway, 'request');
    const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob').mockResolvedValue(new Blob(['original']));
    try {
      expect(new TextDecoder().decode(await provider.readFile(resource))).toBe('original');
      expect(raw.mock.calls[0].slice(0, 3)).toEqual([0, path, ownerKey]);
      request.mockResolvedValueOnce({ node: { uri: path, resource_shape: 'file', stat: { size: 9, modified_at_ms: 99, changed_at_ms: 99 } } });
      await files.stat(resource);
      request.mockRejectedValueOnce(new GatewayRequestError(409, 'Conflict', '版本冲突', '/workspace/files/content', 'conflict-request'));
      request.mockResolvedValueOnce({ path, content: 'other writer', revision: changed });
      await expect(provider.writeFile(resource, new TextEncoder().encode('my draft'), { create: false, overwrite: true, unlock: false, atomic: false })).rejects.toThrow('后端文件已变化');
      const write = request.mock.calls.find(([, options]) => options?.method === 'PUT')!;
      expect(JSON.parse(String(write[1]?.body))).toEqual({ content: 'my draft', expected_revision: original });
      expect(write[2]).toBe(ownerKey);
      expect(request.mock.calls.at(-1)?.[1]?.method).not.toBe('PUT');
    } finally { raw.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  test('原 FileService 的范围读取传递精确字节和取消；部分正文不覆盖文本 CAS 基线', async () => {
    const files = fileService(), resource = workspaceFileUri(owner, root + '/bytes.txt', root);
    const provider = files.getProvider(resource.scheme)!;
    if (!provider.readFile || !provider.readFileStream || !provider.writeFile) throw new Error('缺少文件字节能力');
    const full = new Uint8Array([239, 187, 191, 65, 13, 10]);
    const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob').mockResolvedValueOnce(new Blob([full]));
    const request = spyOn(gateway, 'request').mockResolvedValue({ path: root + '/bytes.txt', revision: '3'.repeat(64) });
    const cancellation = new CancellationTokenSource();
    const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
    try {
      // 普通文件入口继续支持远程 HTTP，不依赖只在安全上下文提供的 WebCrypto。
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined });
      expect(await provider.readFile(resource)).toEqual(full);
      raw.mockResolvedValueOnce(new Blob([full.subarray(3, 5)]));
      const partial = await consumeStream(provider.readFileStream(resource, { position: 3, length: 2 }, CancellationToken.None), chunks => chunks.flatMap(chunk => [...chunk]));
      expect(partial).toEqual([65, 13]);
      expect(raw.mock.calls.at(-1)?.[4]).toEqual({ start: 3, end: 4 });
      await provider.writeFile(resource, new TextEncoder().encode('saved'), { create: false, overwrite: true, unlock: false, atomic: false });
      expect(JSON.parse(String(request.mock.calls.at(-1)?.[1]?.body)).expected_revision).toBe(createHash('sha256').update(full).digest('hex'));
      const reading = Promise.withResolvers<Blob>();
      const started = Promise.withResolvers<void>();
      raw.mockImplementationOnce((_port, _path, _route, signal) => {
        signal!.addEventListener('abort', () => reading.reject(new Error('原字节读取已取消')), { once: true });
        started.resolve();
        return reading.promise;
      });
      const pending = consumeStream(provider.readFileStream(resource, {}, cancellation.token), chunks => chunks);
      await started.promise;
      cancellation.cancel();
      await expect(pending).rejects.toThrow('原字节读取已取消');
    } finally {
      if (cryptoDescriptor) Object.defineProperty(globalThis, 'crypto', cryptoDescriptor);
      else Reflect.deleteProperty(globalThis, 'crypto');
      cancellation.dispose(true); raw.mockRestore(); request.mockRestore(); files.dispose();
    }
  });

  for (const stage of ['request', 'body'] as const) {
    test(`取消后迟到的 ${stage} 不交付字节或推进保存基线`, async () => {
      const files = fileService(), resource = workspaceFileUri(owner, root + '/cancelled.txt', root);
      const provider = files.getProvider(resource.scheme)!;
      if (!provider.readFile || !provider.readFileStream || !provider.writeFile) throw new Error('缺少文件字节能力');
      const raw = spyOn(fileApi, 'getWorkspaceRawFileBlob').mockResolvedValue(new Blob(['original']));
      const request = spyOn(gateway, 'request').mockResolvedValue({ path: root + '/cancelled.txt', revision: '3'.repeat(64) });
      const cancellation = new CancellationTokenSource(), started = Promise.withResolvers<void>();
      const response = Promise.withResolvers<Blob>(), body = Promise.withResolvers<ArrayBuffer>();
      try {
        await provider.readFile(resource);
        const before = raw.mock.calls.length;
        if (stage === 'request') {
          raw.mockImplementationOnce(() => { started.resolve(); return response.promise; });
        } else {
          // 字节转换已经脱离 fetch；显式控制此异步边界，验证取消后不会交付旧结果。
          class PendingBlob extends Blob {
            override arrayBuffer(): Promise<ArrayBuffer> { started.resolve(); return body.promise; }
          }
          raw.mockResolvedValueOnce(new PendingBlob());
        }
        const pending = consumeStream(provider.readFileStream(resource, {}, cancellation.token), chunks => chunks);
        await started.promise;
        cancellation.cancel();
        response.resolve(new Blob(['late bytes']));
        body.resolve(new TextEncoder().encode('late bytes').buffer);
        await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
        expect(raw.mock.calls.length - before).toBe(1);
        await provider.writeFile(resource, new TextEncoder().encode('saved'), { create: false, overwrite: true, unlock: false, atomic: false });
        expect(JSON.parse(String(request.mock.calls.at(-1)?.[1]?.body)).expected_revision).toBe(createHash('sha256').update('original').digest('hex'));
      } finally { cancellation.dispose(true); request.mockRestore(); raw.mockRestore(); files.dispose(); }
    });
  }

  test('同根多个文件及重复 watch 共用原 SSE，变化通知不读正文，最后解除才取消', async () => {
    const files = fileService(), first = workspaceFileUri(owner, root + '/dir/a.txt', root);
    const second = workspaceFileUri(owner, root + '/b.txt', root);
    const provider = files.getProvider(first.scheme)!;
    const changes: IFileChange[][] = [];
    const listener = provider.onDidChangeFile(batch => changes.push([...batch]));
    const started = Promise.withResolvers<NonNullable<Parameters<typeof fileEvents.streamWorkspaceFileEvents>[2]>>();
    const request = spyOn(gateway, 'request');
    const stream = spyOn(fileEvents, 'streamWorkspaceFileEvents').mockImplementation(async (_port, _paths, options) => {
      if (!options?.signal) throw new Error('监听缺少生命周期');
      started.resolve(options);
      await new Promise<void>(done => options.signal!.addEventListener('abort', () => done(), { once: true }));
    });
    const handles = [first, second, first].map(resource => provider.watch(resource, { recursive: false, excludes: [] }));
    try {
      const options = await started.promise;
      expect(stream.mock.calls).toHaveLength(1);
      expect(stream.mock.calls[0].slice(0, 2)).toEqual([0, [root]]);
      expect(options.workspaceId).toBe(ownerKey);
      options.onConnected?.();
      expect(changes).toEqual([]);
      options.onConnected?.();
      expect(changes.pop()?.map(change => change.resource.toString())).toEqual([first.toString(), second.toString()]);
      options.onBatch?.({ changes: [{ kind: 'edit', path: root + '/dir/a.txt' },
        { kind: 'edit', path: root + '/not-open.txt' }, { kind: 'edit', path: root.replace('workspace-a', 'workspace-b') + '/b.txt' }],
        overflow: false, directories: [], event_id: 1, invalidated_roots: [], snapshots: [] });
      expect(changes.pop()?.map(change => [change.resource.toString(), change.type])).toEqual([[first.toString(), FileChangeType.UPDATED]]);
      options.onBatch?.({ changes: [{ kind: 'delete', path: root + '/dir' }], overflow: false,
        directories: [], event_id: 2, invalidated_roots: [], snapshots: [] });
      expect(changes.pop()?.map(change => [change.resource.toString(), change.type])).toEqual([
        [workspaceFileUri(owner, root + '/dir', root).toString(), FileChangeType.DELETED],
      ]);
      options.onBatch?.({ changes: [], overflow: true, directories: [], event_id: 3, invalidated_roots: [root], snapshots: [] });
      expect(changes.pop()?.map(change => change.resource.toString())).toEqual([first.toString(), second.toString()]);
      expect(request.mock.calls).toEqual([]);
      handles[0].dispose(); handles[2].dispose();
      expect(options.signal?.aborted).toBe(false);
      handles[1].dispose();
      expect(options.signal?.aborted).toBe(true);
      options.onBatch?.({ changes: [{ kind: 'edit', path: root + '/b.txt' }], overflow: false,
        directories: [], event_id: 4, invalidated_roots: [], snapshots: [] });
      expect(changes).toEqual([]);
    } finally {
      for (const handle of handles) handle.dispose();
      listener.dispose(); stream.mockRestore(); request.mockRestore(); files.dispose();
    }
  });

  test('watch 立即关闭取消原订阅，迟到失败不污染新代，当前流失败沿原错误事件暴露', async () => {
    const files = fileService(), resource = workspaceFileUri(owner, root + '/x.txt', root);
    const provider = files.getProvider(resource.scheme)!;
    const firstStream = Promise.withResolvers<void>(), notified = Promise.withResolvers<string>();
    const stream = spyOn(fileEvents, 'streamWorkspaceFileEvents').mockImplementationOnce(() => firstStream.promise)
      .mockRejectedValue(new Error('原 SSE 失败：watch-request'));
    const listener = provider.onDidWatchError!(error => notified.resolve(error));
    const first = provider.watch(resource, { recursive: false, excludes: [] });
    try {
      first.dispose();
      expect(stream.mock.calls[0][2]?.workspaceId).toBe(ownerKey);
      expect(stream.mock.calls[0][2]?.signal?.aborted).toBe(true);
      firstStream.reject(new Error('已取消的旧流失败'));
      await firstStream.promise.catch(() => {}); await Promise.resolve();
      const second = provider.watch(resource, { recursive: false, excludes: [] });
      try { expect(await notified.promise).toContain('原 SSE 失败：watch-request'); }
      finally { second.dispose(); }
    } finally { first.dispose(); listener.dispose(); stream.mockRestore(); files.dispose(); }
  });
});
