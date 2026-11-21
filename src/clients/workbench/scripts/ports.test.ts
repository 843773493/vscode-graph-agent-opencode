import { expect, test, mock } from 'bun:test';
import { PortModel, NEW, type PortApi } from '../src/ports/model.js';
import type { GatewayPortForward } from '../../web/src/types/backend.js';

const forward: GatewayPortForward = { gateway_id: 'gateway', forward_id: 'rule', label: 'SSH',
  listener: { host_id: 'local', address: '127.0.0.1', port: 18080 },
  destination: { host_id: 'remote', address: '127.0.0.1', port: 8080 }, protocol: 'tcp', enabled: true, status: 'active', error: null };
function fixture() {
  let items = [structuredClone(forward)];
  const api: PortApi = {
    list: mock(async () => ({ gateway_id: 'gateway', items: structuredClone(items) })),
    hosts: mock(async () => ({ gateway_id: 'gateway', items: [
      { host_id: 'local', label: 'Gateway Host', description: '', kind: 'local' as const },
      { host_id: 'remote', label: 'SSH Host', description: '', kind: 'ssh' as const },
    ] })),
    save: mock(async (_port, payload, id) => {
      const updated = { ...payload, label: payload.label ?? null, forward_id: id ?? 'new', gateway_id: 'gateway',
        status: payload.enabled ? 'active' as const : 'stopped' as const, error: null };
      items = id ? items.map(item => item.forward_id === id ? updated : item) : [...items, updated];
      return { gateway_id: 'gateway', items: structuredClone(items) };
    }),
    setRunning: mock(async (_port, id, running) => {
      items = items.map(item => item.forward_id === id ? { ...item, enabled: running, status: running ? 'active' : 'stopped' } : item);
      return { gateway_id: 'gateway', items: structuredClone(items) };
    }),
    remove: mock(async (_port, id) => { items = items.filter(item => item.forward_id !== id); return { gateway_id: 'gateway', items: structuredClone(items) }; }),
  };
  return { api, model: new PortModel(api) };
}

test('圆点停止保留同一规则，再启动，删除才移除；成功不附加读取', async () => {
  const { model, api } = fixture();
  await model.refresh();
  await model.toggle('rule'); expect(model.items).toMatchObject([{ forward_id: 'rule', status: 'stopped', enabled: false }]);
  await model.toggle('rule'); expect(model.items).toMatchObject([{ forward_id: 'rule', status: 'active' }]);
  await model.remove('rule'); expect(model.items).toEqual([]);
  expect(api.list).toHaveBeenCalledTimes(1);
  model.dispose();
});

test('新行参数逐格编辑、刷新不丢草稿，合法后保存为停止规则', async () => {
  const { model, api } = fixture(); await model.refresh(); model.add();
  await model.edit(NEW, 'listenerPort', '18081'); await model.edit(NEW, 'destinationHost', 'remote'); await model.edit(NEW, 'destinationPort', '8081');
  await model.refresh(); expect(model.draft?.listenerPort).toBe('18081');
  await model.saveNew(false);
  expect(model.draft).toBeUndefined(); expect(model.items[1]).toMatchObject({ enabled: false, status: 'stopped', listener: { port: 18081 }, destination: { host_id: 'remote', port: 8081 } });
  expect(api.save).toHaveBeenCalledTimes(1); model.dispose();
});

test('无效端口保留新行，错误属于行；修正后启动，清除错误', async () => {
  const { model, api } = fixture(); await model.refresh(); model.add();
  await model.edit(NEW, 'listenerPort', '1e3'); await model.edit(NEW, 'destinationPort', '8080');
  await model.toggle(NEW); expect(api.save).not.toHaveBeenCalled(); expect(model.error(NEW)).not.toBe(''); expect(model.draft).toBeDefined();
  await model.edit(NEW, 'listenerPort', '18082'); await model.toggle(NEW);
  expect(model.items[1].status).toBe('active'); expect(model.error(NEW)).toBe(''); model.dispose();
});

test('编辑已有参数消费完整回执；失败重读真实状态并保存原错误', async () => {
  const { model, api } = fixture(); await model.refresh();
  await model.edit('rule', 'destinationPort', '8090'); expect(model.items[0].destination.port).toBe(8090); expect(api.list).toHaveBeenCalledTimes(1);
  api.setRunning = mock(async () => { throw new Error('SSH authentication failed'); });
  await model.toggle('rule'); expect(api.list).toHaveBeenCalledTimes(2); expect(model.items[0].status).toBe('active'); expect(model.error('rule')).toBe('SSH authentication failed');
  model.dispose();
});

test('两行并发提交按顺序消费回执，不用反复补读解决乱序', async () => {
  const { model, api } = fixture(); await model.refresh(); model.add();
  await model.edit(NEW, 'listenerPort', '18083'); await model.edit(NEW, 'destinationPort', '8080'); await model.saveNew(false);
  await Promise.all([model.edit('rule', 'label', 'first'), model.edit('new', 'label', 'second')]);
  expect(model.items.map(item => item.label)).toEqual(['first', 'second']); expect(api.list).toHaveBeenCalledTimes(1); model.dispose();
});
