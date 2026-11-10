import { randomUUID } from 'node:crypto';
import { setTimeout } from 'node:timers/promises';

/** 通过真实安装入口受理一次；后续只查询原操作。 */
export async function installPlugin(api, request) {
  const operationId = randomUUID();
  let operation = await api('/api/gateway/service-plugins/install', {
    method: 'POST', body: { ...request, operation_id: operationId },
  });
  if (operation.operation_id !== operationId) throw new Error(`插件安装受理回执 operation_id 不匹配：${operationId}`);
  const deadline = performance.now() + 30_000;
  while (operation.status === 'pending' || operation.status === 'running') {
    if (performance.now() >= deadline) throw new Error(`插件安装等待超时：${operationId}`);
    await setTimeout(50);
    operation = await api('/api/gateway/service-plugins/install-operation', {
      method: 'POST', body: { gateway_id: request.gateway_id, operation_id: operationId },
    });
    if (operation.operation_id !== operationId) throw new Error(`插件安装状态 operation_id 不匹配：${operationId}`);
  }
  if (operation.status !== 'succeeded') throw new Error(`插件安装 ${operation.status}: ${operation.error ?? operationId}`);
  return operation;
}
