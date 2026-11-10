import { afterEach, expect, test } from 'bun:test';
import { watchPluginConfigurationSchemas } from '../pluginConfigurationSchemas';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../session/sessionApiFetchMock';

const target = { gateway_id: 'gateway-schema', workspace_id: '12345678-1234-5678-1234-567812345678' };
afterEach(unwrapSessionApiFetch);

test('安装声明按原目标解码，普通断流不伪装成成功', async () => {
  const snapshot = { ...target, plugins: [] }, received: unknown[] = [];
  installSessionApiFetchMock(({ path, method, init }) => {
    expect(path).toBe('/api/gateway/service-plugins/configuration-schemas/events');
    expect(method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual(target);
    return new Response(`event: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n`, {
      headers: { 'content-type': 'text/event-stream' },
    });
  });
  await expect(watchPluginConfigurationSchemas(target, new AbortController().signal, value => received.push(value)))
    .rejects.toThrow('配置声明连接已结束');
  expect(received).toEqual([snapshot]);
});

test.each([
  [{ gateway_id: target.gateway_id, plugins: [] }, '不属于原目标'],
  [{ ...target, gateway_id: 'gateway-other', plugins: [] }, '不属于原目标'],
  [{ ...target, plugins: 'invalid' }, '不符合公开合同'],
])('错误归属或公开结构不交给设置消费者：%j', async (value, message) => {
  const received: unknown[] = [];
  installSessionApiFetchMock(() => new Response(`event: snapshot\ndata: ${JSON.stringify(value)}\n\n`, {
    headers: { 'content-type': 'text/event-stream' },
  }));
  await expect(watchPluginConfigurationSchemas(target, new AbortController().signal, value => received.push(value)))
    .rejects.toThrow(message);
  expect(received).toEqual([]);
});
