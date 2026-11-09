import { fromJsonString, toJson } from '@bufbuild/protobuf';
import { ValueSchema } from '@bufbuild/protobuf/wkt';
import { requestGatewayResponse, DEFAULT_API_REQUEST_TIMEOUT_MS } from './http.js';
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from '../sse/sseClient.js';
import { validatePluginCommandSnapshotDTO } from '../../../../shared/sseRuntimeValidators.js';
import { PluginCommandSnapshotDTO, type PluginContributionTarget, type PluginCommandCallRequest } from '../types/protocol_generated/boxteam/gateway/v2/plugin_commands.js';
import { PluginCommandEventDTOSchema } from '../types/protocol_buf_generated/boxteam/gateway/v2/plugin_commands_pb.js';

const path = '/api/gateway/service-plugins/commands';
const jsonHeaders = { 'Content-Type': 'application/json' };

export async function watchPluginCommands(target: PluginContributionTarget, signal: AbortSignal,
  onSnapshot: (snapshot: PluginCommandSnapshotDTO) => void): Promise<void> {
  const response = await requestGatewayResponse(0, path + '/events', {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify(target), signal,
  });
  await consumeSseResponse(response, { signal, events: {
    snapshot: defineSseEvent((data, frame) => {
      const value = decodeJsonSseData(data, frame);
      if (!validatePluginCommandSnapshotDTO(value)) throw new Error('插件贡献快照不符合公开合同：' + JSON.stringify(validatePluginCommandSnapshotDTO.errors));
      // HTTP 可空值由既有生成的 JSON 适配器收敛为 TS optional。
      return PluginCommandSnapshotDTO.fromJSON(value);
    }, onSnapshot),
  } });
  if (!signal.aborted) throw new Error('插件贡献连接已结束，请刷新重新连接');
}

export async function callPluginCommand(target: Omit<PluginCommandCallRequest, 'call_id' | 'arguments' | 'remaining_ms'>, args: unknown[], options: {
  signal: AbortSignal;
  onStarted: () => void;
  onProgress: (completed?: number, total?: number, message?: string) => void;
  onIdentity: (cancel: () => Promise<void>) => void;
}): Promise<unknown> {
  if (args.length > 1 || args.length === 1 && (args[0] === null || typeof args[0] !== 'object' || Array.isArray(args[0]))) {
    throw new Error('插件命令参数必须是单个 JSON 对象');
  }
  const identity = { ...target, call_id: crypto.randomUUID() };
  options.onIdentity(async () => {
    const response = await requestGatewayResponse(0, path + '/cancel', {
      method: 'POST', headers: jsonHeaders, body: JSON.stringify(identity), signal: options.signal,
    });
    // 取消沿原调用收取终态，不把管理回执当作执行已取消。
    await response.arrayBuffer();
  });
  const response = await requestGatewayResponse(0, path + '/call', { method: 'POST', headers: jsonHeaders,
    body: JSON.stringify({ ...identity, arguments: args[0] ?? {}, remaining_ms: DEFAULT_API_REQUEST_TIMEOUT_MS }), signal: options.signal,
  });
  const requestId = response.headers.get('X-Request-ID');
  if (!requestId || !response.body) {
    await response.body?.cancel();
    throw new Error('插件命令响应缺少请求身份或正文');
  }
  const reader = response.body.pipeThrough(new TextDecoderStream(), { signal: options.signal }).getReader();
  let buffer = '', sequence = 0, terminal = false, result: unknown;
  const accept = (line: string) => {
    const event = fromJsonString(PluginCommandEventDTOSchema, line);
    if (terminal || event.instanceId !== identity.instance_id || event.registrationId !== identity.registration_id
      || event.commandId !== identity.command_id || event.callId !== identity.call_id || event.requestId !== requestId
      || event.sequence !== ++sequence) throw new Error('插件命令回执不属于原调用或顺序无效');
    if (sequence === 1 && event.kind !== 'started') throw new Error('插件命令缺少 started 回执');
    switch (event.kind) {
      case 'started': if (sequence !== 1) throw new Error('插件命令重复 started'); options.onStarted(); break;
      case 'progress': options.onProgress(event.completed, event.total, event.message); break;
      case 'result': terminal = true; result = event.result ? toJson(ValueSchema, event.result) : null; break;
      case 'cancelled': terminal = true; throw new Error('插件命令已取消');
      case 'error': terminal = true; throw new Error(`${event.message ?? '插件命令失败'}（${event.errorCode ?? 'unknown'}，${requestId}）`);
      default: throw new Error('插件命令返回未知事件：' + event.kind);
    }
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let newline: number;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        if (line.trim()) accept(line);
      }
    }
    if (buffer.trim()) accept(buffer);
    if (!terminal) throw new Error(`插件命令连接中断，执行结果未知；不会重放（${requestId}）`);
    return result;
  } finally {
    try { await reader.cancel(); } finally { reader.releaseLock(); }
  }
}
