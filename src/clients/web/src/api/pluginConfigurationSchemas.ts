import { requestGatewayResponse } from './http.js';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../sse/sseClient.js';
import { validatePluginConfigurationSchemasDTO } from '../../../../shared/sseRuntimeValidators.js';
import { PluginConfigurationSchemasDTO, type PluginConfigurationSchemasRequest } from '../types/protocol_generated/boxteam/gateway/v2/service_plugin.js';

/** 只观察目标安装声明，完整快照由原安装 owner 提供，不启动插件。 */
export async function watchPluginConfigurationSchemas(target: PluginConfigurationSchemasRequest, signal: AbortSignal,
  onSnapshot: (snapshot: PluginConfigurationSchemasDTO) => void): Promise<void> {
  const response = await requestGatewayResponse(0, '/api/gateway/service-plugins/configuration-schemas/events', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(target), signal,
  });
  await consumeSseResponse(response, { signal, events: {
    snapshot: defineSseEvent((data, frame) => {
      const value = decodeJsonSseData(data, frame);
      if (!validatePluginConfigurationSchemasDTO(value)) throw new Error('配置声明事件不符合公开合同：' + JSON.stringify(validatePluginConfigurationSchemasDTO.errors));
      return PluginConfigurationSchemasDTO.fromJSON(value);
    }, snapshot => {
      if (snapshot.gateway_id !== target.gateway_id || snapshot.workspace_id !== target.workspace_id) {
        throw new Error('配置声明事件不属于原目标');
      }
      onSnapshot(snapshot);
    }),
  } });
  if (!signal.aborted) throw new Error('配置声明连接已结束，请重新打开设置以连接');
}
