import { describe, expect, test, spyOn } from 'bun:test';
import { ThreadInput, ThreadSerializer, UnavailableThreadInput } from '../src/threadInput.js';
import { gateway, type ThreadOwner } from '../src/agentApi.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import type { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';

const owner: ThreadOwner = { gatewayId: 'gateway_00000000000000000000000000000001', workspaceId: '00000000-0000-4000-8000-000000000001',
  sessionId: 'session-a', threadId: 'thread-child', workspaceKey: '', title: '原名称' };
const workspaceKey = gatewayWorkspaceKey(owner);
const serializer = new ThreadSerializer();
// serializer 不使用实例化服务；测试只提供该未消费的位置参数。
const instantiation = undefined as unknown as IInstantiationService;

describe('Thread 编辑器稳定身份', () => {
  test('改名与工作区键解析不复制标签，不同 Gateway 和精确 child 保持隔离', () => {
    const input = new ThreadInput({ ...owner });
    expect(input.matches(new ThreadInput({ ...owner, workspaceKey, title: '新名称' }))).toBe(true);
    expect(input.matches(input.toUntyped())).toBe(true);
    expect(input.matches(new ThreadInput({ ...owner, gatewayId: 'gateway_00000000000000000000000000000002' }))).toBe(false);
    expect(input.matches(new ThreadInput({ ...owner, threadId: 'main-thread' }))).toBe(false);
    expect(input.resource.query).toBe('');
    const restored = ThreadInput.fromResource(input.resource);
    expect(restored.owner).toMatchObject({ ...owner, workspaceKey: '', title: owner.threadId });
    expect(() => ThreadInput.fromResource(URI.from({ scheme: 'boxteam-thread', authority: owner.workspaceId,
      path: `/${owner.sessionId}/${owner.threadId}`, query: 'route=old-route' }))).toThrow('稳定 Gateway');
  });

  test('序列化仅保存稳定目标并保留每组展示态，分屏复制互不覆盖', () => {
    const input = new ThreadInput({ ...owner, workspaceKey });
    input.resourcesVisible = true; input.resourceTab = 'changes'; input.selectedChangesetId = 'turn:chosen'; input.fileTreeExpandedPaths = ['src'];
    const stored = serializer.serialize(input)!;
    expect(JSON.parse(stored)).toEqual({ version: 2,
      target: { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId, sessionId: owner.sessionId, threadId: owner.threadId },
      title: owner.title, viewState: input.getViewState() });
    expect(stored).not.toContain('workspaceKey');
    const restored = serializer.deserialize(instantiation, stored);
    expect(restored).toBeInstanceOf(ThreadInput);
    if (!(restored instanceof ThreadInput)) throw new Error('恢复类型错误');
    expect(restored.owner.workspaceKey).toBe('');
    expect(restored.getViewState()).toEqual(input.getViewState());
    const copy = restored.copy(); copy.fileTreeExpandedPaths.push('test'); copy.selectedChangesetId = 'all';
    expect(restored.selectedChangesetId).toBe('turn:chosen');
    expect(restored.fileTreeExpandedPaths).toEqual(['src']);
    expect(copy.matches(restored)).toBe(true);
    input.restorable = false;
    expect(serializer.canSerialize(input)).toBe(false);
    expect(serializer.serialize(input)).toBeUndefined();
  });

  test('旧记录保留为可关闭错误，重写后也不猜测 Gateway', () => {
    const legacy = { owner: { routeId: 'old-route', workspaceId: 'workspace-a', sessionId: owner.sessionId,
      threadId: owner.threadId, title: owner.title }, viewState: { resourcesVisible: true } };
    const restored = serializer.deserialize(instantiation, JSON.stringify(legacy));
    expect(restored).toBeInstanceOf(UnavailableThreadInput);
    if (!(restored instanceof UnavailableThreadInput)) throw new Error('旧记录被错误恢复');
    expect(restored.stored).toEqual(legacy);
    expect(restored.reason).toContain('尚未确认 Gateway 身份');
    expect(serializer.deserialize(instantiation, serializer.serialize(restored)!)).toBeInstanceOf(UnavailableThreadInput);
  });

  test.each([
    ['{"version":3,"title":"未来记录"}', '不支持的 Thread 编辑器恢复版本'],
    ['{"version":2,"title":"原记录","target":{}}', '缺少精确 owner'],
    ['{"version":2,"target":null}', '缺少精确 owner'],
    ['null', '必须是对象'],
    ['[]', '必须是对象'],
    ['false', '必须是对象'],
    ['"旧正文"', '必须是对象'],
    ['{"version":2,', 'JSON 损坏'],
  ])('损坏或未知记录保留原件并能重复恢复：%s', (serialized, reason) => {
    let restored = serializer.deserialize(instantiation, serialized);
    const original = serialized === '{"version":2,' ? serialized : JSON.parse(serialized);
    for (let round = 0; round < 3; round++) {
      expect(restored).toBeInstanceOf(UnavailableThreadInput);
      if (!(restored instanceof UnavailableThreadInput)) throw new Error('损坏记录被错误恢复为业务目标');
      expect(restored.resource).toBeUndefined();
      expect(restored.stored).toEqual(original);
      expect(restored.reason).toContain(reason);
      expect(restored.getName()).toBeTruthy();
      expect(serializer.canSerialize(restored)).toBe(true);
      restored = serializer.deserialize(instantiation, serializer.serialize(restored)!);
    }
  });

  test('并发只解析一次，重解与失败后重试保持归属并透传失效错误', async () => {
    const input = ThreadInput.fromResource(new ThreadInput({ ...owner, workspaceKey }).resource);
    expect(input.owner.workspaceKey).toBe('');
    const resolve = spyOn(gateway, 'resolveWorkspace').mockResolvedValue(workspaceKey);
    try {
      const [first, second] = await Promise.all([input.resolveOwner(), input.resolveOwner()]);
      expect(resolve).toHaveBeenCalledTimes(1);
      expect(first).toBe(second);
      expect(first).toBe(input.owner);
      expect(first.workspaceKey).toBe(workspaceKey);
      const reopened = await input.resolveOwner();
      expect(resolve).toHaveBeenCalledTimes(2);
      expect(reopened).toBe(first);
      expect(reopened.workspaceKey).toBe(workspaceKey);
      const failure = new Error('目标 Gateway／UUID 缺少唯一已登记工作区');
      resolve.mockRejectedValueOnce(failure);
      await expect(input.resolveOwner()).rejects.toBe(failure);
      expect(resolve).toHaveBeenCalledTimes(3);
      expect(input.owner.workspaceKey).toBe(workspaceKey);
      const retried = await input.resolveOwner();
      expect(resolve).toHaveBeenCalledTimes(4);
      expect(retried).toBe(first);
      expect(retried.workspaceKey).toBe(workspaceKey);
    } finally { resolve.mockRestore(); }
  });
});
