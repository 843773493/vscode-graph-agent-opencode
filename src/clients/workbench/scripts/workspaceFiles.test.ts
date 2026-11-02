import { describe, expect, test, spyOn } from 'bun:test';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { joinPath, relativePath, isEqualOrParent } from '../upstream/src/vs/base/common/resources.js';
import { FileService } from '../upstream/src/vs/platform/files/common/fileService.js';
import { FileType, FileChangeType, type IFileChange } from '../upstream/src/vs/platform/files/common/files.js';
import { NullLogService } from '../upstream/src/vs/platform/log/common/log.js';
import { gateway, GatewayRequestError } from '../src/agentApi.js';
import { installBackendFiles, workspaceFileUri, workspaceFileSource } from '../src/workspaceFiles.js';
import * as fileEvents from '../../web/src/api/stream/workspaceFileEvents.js';

const owner = { gatewayId: 'gateway_file_test', workspaceId: 'workspace-a' };
const root = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;

// 通过原版 FileService 验证实际 provider 调用，不只测试投影函数。
function fileService() {
  const files = new FileService(new NullLogService());
  installBackendFiles(files);
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
  });

  test('目录和二进制 stat 只 describe，缺失和权限错误由原服务返回', async () => {
    const files = fileService();
    const resolve = spyOn(gateway, 'resolveWorkspace').mockResolvedValue('new-route');
    const request = spyOn(gateway, 'request');
    try {
      for (const [path, shape, expectedType] of [[root, 'directory', FileType.Directory], [root + '/image.bin', 'file', FileType.File]] as const) {
        request.mockResolvedValueOnce({ node: { uri: path, resource_shape: shape,
          stat: { size: 73, modified_at_ms: 1200, changed_at_ms: 1100 } } });
        const stat = await files.stat(workspaceFileUri(owner, path, root));
        expect(stat).toMatchObject({ isDirectory: expectedType === FileType.Directory, isFile: expectedType === FileType.File, size: 73, mtime: 1200, ctime: 1100 });
        const [endpoint, options, routeId] = request.mock.calls.at(-1)!;
        expect(endpoint).toBe('/api/v1/resources/navigation');
        expect(options?.method).toBe('POST');
        expect(JSON.parse(String(options?.body))).toEqual({ uri: path, operation: 'describe' });
        expect(routeId).toBe('new-route');
      }
      request.mockRejectedValueOnce(new GatewayRequestError(404, 'Not Found', '目录不存在', '/resources/navigation', 'missing-request'));
      await expect(files.stat(workspaceFileUri(owner, root + '/missing', root))).rejects.toThrow('目录不存在');
      request.mockRejectedValueOnce(new GatewayRequestError(403, 'Forbidden', '没有访问权限', '/resources/navigation', 'denied-request'));
      await expect(files.stat(workspaceFileUri(owner, root + '/denied', root))).rejects.toThrow('没有访问权限');
      expect(resolve.mock.calls.every(([target]) => target.gatewayId === owner.gatewayId && target.workspaceId === owner.workspaceId)).toBe(true);
    } finally { resolve.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  test('stat 不推进读入版本，路由改变后保存仍以原内容 CAS 拒绝覆盖', async () => {
    const files = fileService();
    const path = root + '/x.txt';
    const resource = workspaceFileUri(owner, path, root);
    const provider = files.getProvider(resource.scheme);
    if (!provider?.readFile || !provider.writeFile) throw new Error('文件 provider 未登记');
    const original = '1'.repeat(64), changed = '2'.repeat(64);
    const resolve = spyOn(gateway, 'resolveWorkspace').mockResolvedValue('old-route');
    const request = spyOn(gateway, 'request');
    try {
      request.mockResolvedValueOnce({ path, content: 'original', revision: original });
      expect(new TextDecoder().decode(await provider.readFile(resource))).toBe('original');
      resolve.mockResolvedValue('replacement-route');
      request.mockResolvedValueOnce({ node: { uri: path, resource_shape: 'file', stat: { size: 9, modified_at_ms: 99, changed_at_ms: 99 } } });
      await files.stat(resource);
      request.mockRejectedValueOnce(new GatewayRequestError(409, 'Conflict', '版本冲突', '/workspace/files/content', 'conflict-request'));
      request.mockResolvedValueOnce({ path, content: 'other writer', revision: changed });
      await expect(provider.writeFile(resource, new TextEncoder().encode('my draft'), { create: false, overwrite: true, unlock: false, atomic: false })).rejects.toThrow('后端文件已变化');
      const write = request.mock.calls.find(([, options]) => options?.method === 'PUT')!;
      expect(JSON.parse(String(write[1]?.body))).toEqual({ content: 'my draft', expected_revision: original });
      expect(write[2]).toBe('replacement-route');
      expect(request.mock.calls.at(-1)?.[1]?.method).not.toBe('PUT');
    } finally { resolve.mockRestore(); request.mockRestore(); files.dispose(); }
  });

  test('同根多个文件及重复 watch 共用原 SSE，变化通知不读正文，最后解除才取消', async () => {
    const files = fileService(), first = workspaceFileUri(owner, root + '/dir/a.txt', root);
    const second = workspaceFileUri(owner, root + '/b.txt', root);
    const provider = files.getProvider(first.scheme)!;
    const changes: IFileChange[][] = [];
    const listener = provider.onDidChangeFile(batch => changes.push([...batch]));
    const started = Promise.withResolvers<NonNullable<Parameters<typeof fileEvents.streamWorkspaceFileEvents>[2]>>();
    const resolve = spyOn(gateway, 'resolveWorkspace').mockResolvedValue('watch-route');
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
      expect(options.workspaceId).toBe('watch-route');
      expect(resolve.mock.calls[0][1]).toBe(options.signal);
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
      listener.dispose(); stream.mockRestore(); resolve.mockRestore(); request.mockRestore(); files.dispose();
    }
  });

  test('watch 在路由解析前关闭不启动迟到订阅，流失败沿原错误事件暴露', async () => {
    const files = fileService(), resource = workspaceFileUri(owner, root + '/x.txt', root);
    const provider = files.getProvider(resource.scheme)!;
    const route = Promise.withResolvers<string>(), notified = Promise.withResolvers<string>();
    const resolve = spyOn(gateway, 'resolveWorkspace').mockImplementation(() => route.promise);
    const stream = spyOn(fileEvents, 'streamWorkspaceFileEvents').mockRejectedValue(new Error('原 SSE 失败：watch-request'));
    const listener = provider.onDidWatchError!(error => notified.resolve(error));
    const first = provider.watch(resource, { recursive: false, excludes: [] });
    try {
      first.dispose(); route.resolve('late-route');
      await route.promise; await Promise.resolve();
      expect(stream.mock.calls).toEqual([]);
      const second = provider.watch(resource, { recursive: false, excludes: [] });
      try { expect(await notified.promise).toContain('原 SSE 失败：watch-request'); }
      finally { second.dispose(); }
    } finally { first.dispose(); listener.dispose(); stream.mockRestore(); resolve.mockRestore(); files.dispose(); }
  });
});
