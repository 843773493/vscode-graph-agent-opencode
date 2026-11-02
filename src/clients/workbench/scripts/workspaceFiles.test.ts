import { describe, expect, test, spyOn } from 'bun:test';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { joinPath, relativePath, isEqualOrParent } from '../upstream/src/vs/base/common/resources.js';
import { FileService } from '../upstream/src/vs/platform/files/common/fileService.js';
import { FileType } from '../upstream/src/vs/platform/files/common/files.js';
import { NullLogService } from '../upstream/src/vs/platform/log/common/log.js';
import { gateway, GatewayRequestError } from '../src/agentApi.js';
import { installBackendFiles, workspaceFileUri, workspaceFileSource } from '../src/workspaceFiles.js';

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
});
