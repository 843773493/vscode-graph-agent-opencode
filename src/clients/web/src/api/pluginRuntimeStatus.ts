import { requestGatewayResponse } from './http.js';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../sse/sseClient.js';
import { validatePluginRuntimeStatusDTO } from '../../../../shared/sseRuntimeValidators.js';
import { PluginRuntimeStatusDTO, type PluginTarget } from '../types/protocol_generated/boxteam/gateway/v2/service_plugin.js';

/** 观察目标 Gateway 的完整运行状态快照；订阅取消即释放原 SSE 响应。 */
export async function watchPluginRuntimeStatus(target: PluginTarget, signal: AbortSignal,
  onSnapshot: (snapshot: PluginRuntimeStatusDTO) => void): Promise<void> {
  const response = await requestGatewayResponse(0, '/api/gateway/service-plugins/status/events', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(target), signal,
  });
  await consumeSseResponse(response, { signal, events: {
    snapshot: defineSseEvent((data, frame) => {
      const value = decodeJsonSseData(data, frame);
      if (!validatePluginRuntimeStatusDTO(value)) {
        throw new Error('插件运行状态事件不符合公开合同：' + JSON.stringify(validatePluginRuntimeStatusDTO.errors));
      }
      return PluginRuntimeStatusDTO.fromJSON(value);
    }, snapshot => {
      if (snapshot.plugins.some(slot => slot.gateway_id !== target.gateway_id)) {
        throw new Error('插件运行状态事件包含其它 Gateway 的实例');
      }
      onSnapshot(snapshot);
    }),
  } });
  if (!signal.aborted) throw new Error('插件运行状态连接已结束，请重新打开管理视图');
}
