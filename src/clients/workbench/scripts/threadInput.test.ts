import { describe, expect, test, spyOn } from 'bun:test';
import { ThreadInput, ThreadSerializer, UnavailableThreadInput } from '../src/threadInput.js';
import { gateway, type ThreadOwner } from '../src/agentApi.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';

const owner: ThreadOwner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a', sessionId: 'session-a', threadId: 'thread-child', routeId: 'old-route', title: '原名称' };
const serializer = new ThreadSerializer();
// serializer 不使用实例化服务；测试只提供该未消费的位置参数。
const instantiation = undefined as unknown as IInstantiationService;

describe('Thread 编辑器稳定身份', () => {
  test('改名与路由改变不复制标签，不同 Gateway 和精确 child 保持隔离', () => {
    const input = new ThreadInput({ ...owner });
    expect(input.matches(new ThreadInput({ ...owner, routeId: 'new-route', title: '新名称' }))).toBe(true);
    expect(input.matches(input.toUntyped())).toBe(true);
    expect(input.matches(new ThreadInput({ ...owner, gatewayId: 'gateway-b' }))).toBe(false);
    expect(input.matches(new ThreadInput({ ...owner, threadId: 'main-thread' }))).toBe(false);
    expect(input.resource.query).toBe('');
    const restored = ThreadInput.fromResource(input.resource);
    expect(restored.owner).toMatchObject({ ...owner, routeId: '', title: owner.threadId });
    expect(() => ThreadInput.fromResource(URI.from({ scheme: 'boxteam-thread', authority: owner.workspaceId,
      path: `/${owner.sessionId}/${owner.threadId}`, query: 'route=old-route' }))).toThrow('稳定 Gateway');
  });

  test('序列化仅保存稳定目标并保留每组展示态，分屏复制互不覆盖', () => {
    const input = new ThreadInput({ ...owner });
    input.resourcesVisible = true; input.resourceTab = 'debug'; input.fileTreeExpandedPaths = ['src'];
    const stored = serializer.serialize(input)!;
    expect(JSON.parse(stored)).toEqual({ version: 2,
      target: { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId, sessionId: owner.sessionId, threadId: owner.threadId },
      title: owner.title, viewState: input.getViewState() });
    expect(stored).not.toContain('routeId');
    const restored = serializer.deserialize(instantiation, stored);
    expect(restored).toBeInstanceOf(ThreadInput);
    if (!(restored instanceof ThreadInput)) throw new Error('恢复类型错误');
    expect(restored.owner.routeId).toBe('');
    expect(restored.getViewState()).toEqual(input.getViewState());
    const copy = restored.copy(); copy.fileTreeExpandedPaths.push('test');
    expect(restored.fileTreeExpandedPaths).toEqual(['src']);
    expect(copy.matches(restored)).toBe(true);
    input.restorable = false;
    expect(serializer.canSerialize(input)).toBe(false);
    expect(serializer.serialize(input)).toBeUndefined();
  });

  test('旧记录保留为可关闭错误，重写后也不猜测 Gateway', () => {
    const legacy = { owner: { routeId: owner.routeId, workspaceId: owner.workspaceId, sessionId: owner.sessionId,
      threadId: owner.threadId, title: owner.title }, viewState: { resourcesVisible: true } };
    const restored = serializer.deserialize(instantiation, JSON.stringify(legacy));
    expect(restored).toBeInstanceOf(UnavailableThreadInput);
    if (!(restored instanceof UnavailableThreadInput)) throw new Error('旧记录被错误恢复');
    expect(restored.stored).toEqual(legacy);
    expect(restored.reason).toContain('缺少 Gateway 身份');
    expect(serializer.deserialize(instantiation, serializer.serialize(restored)!)).toBeInstanceOf(UnavailableThreadInput);
    expect(() => serializer.deserialize(instantiation, '{"version":3}')).toThrow('不支持');
    expect(() => serializer.deserialize(instantiation, '{"version":2,"target":{}}')).toThrow('缺少精确 owner');
  });

  test('同时打开与导航只解析一次，在下次重开重解路由并透传失效错误', async () => {
    const input = new ThreadInput({ ...owner });
    const resolve = spyOn(gateway, 'resolveWorkspace').mockResolvedValue('current-route');
    try {
      const [first, second] = await Promise.all([input.resolveOwner(), input.resolveOwner()]);
      expect(resolve).toHaveBeenCalledTimes(1);
      expect(first).toBe(second);
      expect(first.routeId).toBe('current-route');
      resolve.mockResolvedValueOnce('changed-route');
      expect((await input.resolveOwner()).routeId).toBe('changed-route');
      resolve.mockRejectedValueOnce(new Error('缺少唯一的已登记路由'));
      await expect(input.resolveOwner()).rejects.toThrow('缺少唯一的已登记路由');
      expect(input.owner.routeId).toBe('changed-route');
    } finally { resolve.mockRestore(); }
  });
});
