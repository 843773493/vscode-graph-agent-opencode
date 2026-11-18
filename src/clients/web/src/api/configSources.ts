import type { APIResponse, ConfigSourcesDTO } from "../types/backend";
import type { ConfigReloadStatusDTO, ConfigSourceDocumentDTO, ConfigSourceEditRequest, ConfigSourcePreflightDTO, LlmDiscoverRequest, LlmDiscoverResultDTO, LlmTestRequest, LlmTestStartDTO, LlmTestDeltaDTO, LlmTestResultDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import { LlmProbeObservationDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { ModelReferenceDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestGatewayResponse, requestJson, unwrapApiData } from "./http";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent, SseProtocolError } from "../sse/sseClient";
import { validateLlmTestStartDTO, validateLlmTestDeltaDTO, validateLlmTestResultDTO } from "../../../../shared/sseRuntimeValidators.js";

function assertProbeTarget(observation: LlmProbeObservationDTO | undefined, input: LlmDiscoverRequest): void {
  if (observation?.gateway_id !== input.gateway_id || observation.workspace_id !== input.workspace_id
    || observation.provider_id !== input.provider_id || observation.mode !== input.mode
    || observation.edit_version !== input.edit_version || (observation.source_key ?? undefined) !== input.source_key) {
    throw new SseProtocolError("模型探测回执不属于原目标、连接或编辑版本");
  }
}

function assertDocumentTarget(document: ConfigSourceDocumentDTO, target: GatewayWorkspaceRequestTarget, sourceKey: string,
  idempotencyKey?: string): void {
  if (document.gateway_id !== target.gateway_id || document.workspace_id !== target.workspace_id || document.source_key !== sourceKey
      || (idempotencyKey !== undefined && document.edit && document.edit.idempotency_key !== idempotencyKey)) {
    throw new Error("配置来源回执不属于原 Gateway／工作区、来源或提交");
  }
}

export async function listConfigSources(port: number, target: GatewayWorkspaceRequestTarget, signal?: AbortSignal): Promise<ConfigSourcesDTO> {
  return unwrapApiData(await requestJson<APIResponse<ConfigSourcesDTO>>(port, "/api/v1/config/sources", {
    headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store", signal,
  }));
}

/** 一次性目录请求使用独立的完整期限；不保存来源、不重放上游操作。 */
export async function discoverLlmModels(port: number, target: GatewayWorkspaceRequestTarget, input: LlmDiscoverRequest,
  signal?: AbortSignal): Promise<LlmDiscoverResultDTO> {
  const route = { ...target };
  if (input.gateway_id !== route.gateway_id || input.workspace_id !== route.workspace_id) throw new Error("模型发现请求不属于原 Gateway／工作区");
  const result = unwrapApiData(await requestJson<APIResponse<LlmDiscoverResultDTO>>(port, "/api/v1/config/llm/discover", {
    method: "POST", headers: gatewayWorkspaceRequestHeaders(route), body: JSON.stringify(input),
    cache: "no-store", timeoutMs: 20_000, signal,
  }));
  assertProbeTarget(result.observation, input);
  return result;
}

/** 单次 POST 从取头到终态共用总期限；任何失败均不重放推理请求。 */
export async function testLlmModel(port: number, target: GatewayWorkspaceRequestTarget, input: LlmTestRequest,
  options: { signal: AbortSignal; onStart: (event: LlmTestStartDTO) => void; onDelta: (event: LlmTestDeltaDTO) => void },
): Promise<LlmTestResultDTO> {
  const route = { ...target };
  if (input.gateway_id !== route.gateway_id || input.workspace_id !== route.workspace_id) throw new Error("模型测试请求不属于原 Gateway／工作区");
  const abort = new AbortController();
  const cancel = () => abort.abort(options.signal.reason);
  options.signal.addEventListener("abort", cancel, { once: true });
  if (options.signal.aborted) cancel();
  const timer = setTimeout(() => abort.abort(new DOMException("模型测试总期限已到", "TimeoutError")), (input.timeout_seconds + 5) * 1000);
  let response: Response | undefined;
  try {
    response = await requestGatewayResponse(port, "/api/v1/config/llm/test", {
      method: "POST", headers: { ...gatewayWorkspaceRequestHeaders(route), "Content-Type": "application/json" }, body: JSON.stringify(input),
      cache: "no-store", timeoutMs: 0, signal: abort.signal,
    });
    const requestId = response.headers.get("X-Request-ID");
    if (!requestId) throw new SseProtocolError("模型测试响应缺少 request_id");
    let start: LlmTestStartDTO | undefined;
    let result: LlmTestResultDTO | undefined;
    const assertIdentity = (event: LlmTestStartDTO) => {
      assertProbeTarget(event.observation, input);
      if (event.request_id !== requestId || event.model_ref?.provider_id !== input.provider_id
        || event.model_ref.model_id !== input.model_id || input.streaming !== undefined && event.streaming !== input.streaming) {
        throw new SseProtocolError("模型测试事件的请求、模型或模式不一致");
      }
    };
    await consumeSseResponse(response, { signal: abort.signal, events: {
      start: defineSseEvent((data, frame) => {
        const event: unknown = decodeJsonSseData(data, frame);
        if (!validateLlmTestStartDTO(event)) throw new SseProtocolError("模型测试 start 结构无效");
        return event as LlmTestStartDTO;
      }, event => {
        if (start || result) throw new SseProtocolError("模型测试重复 start");
        assertIdentity(event);
        start = event;
        options.onStart(event);
      }),
      delta: defineSseEvent((data, frame) => {
        const event: unknown = decodeJsonSseData(data, frame);
        if (!validateLlmTestDeltaDTO(event)) throw new SseProtocolError("模型测试 delta 结构无效");
        return event as LlmTestDeltaDTO;
      }, event => {
        if (!start || result || event.request_id !== requestId) throw new SseProtocolError("模型测试 delta 顺序或请求不一致");
        options.onDelta(event);
      }),
      result: defineSseEvent((data, frame) => {
        const event: unknown = decodeJsonSseData(data, frame);
        if (!validateLlmTestResultDTO(event)) throw new SseProtocolError("模型测试 result 结构无效");
        return event as LlmTestResultDTO;
      }, event => {
        if (!start || result) throw new SseProtocolError("模型测试终态顺序无效");
        assertIdentity(event);
        // 生成映射按协议字段排序，JSON 对象的键顺序不参与观察版本身份。
        const observationJson = (value: LlmProbeObservationDTO | undefined) => {
          const normalized = LlmProbeObservationDTO.fromJSON(value);
          normalized.source_baseline = Object.fromEntries(Object.entries(normalized.source_baseline).sort(([left], [right]) => left.localeCompare(right)));
          return JSON.stringify(LlmProbeObservationDTO.toJSON(normalized));
        };
        if (event.streaming !== start.streaming || observationJson(event.observation) !== observationJson(start.observation)) {
          throw new SseProtocolError("模型测试终态改变了本次冻结观察版本");
        }
        result = event;
      }),
    } });
    abort.signal.throwIfAborted();
    if (!result) throw new SseProtocolError(`模型测试连接结束但没有终态: request_id=${requestId}`);
    return result;
  } catch (error) {
    if (abort.signal.aborted) throw abort.signal.reason;
    throw error;
  } finally {
    clearTimeout(timer);
    options.signal.removeEventListener("abort", cancel);
    if (response?.body && !response.bodyUsed && !response.body.locked) await response.body.cancel();
  }
}

/** 正文与 CAS 只消费原来源 owner；查询提交沿用原幂等键。 */
export async function getConfigSourceDocument(port: number, target: GatewayWorkspaceRequestTarget, sourceKey: string,
  { idempotencyKey, signal }: { idempotencyKey?: string; signal?: AbortSignal } = {}): Promise<ConfigSourceDocumentDTO> {
  const route = { ...target };
  const query = idempotencyKey === undefined ? "" : `?${new URLSearchParams({ idempotency_key: idempotencyKey })}`;
  const document = unwrapApiData(await requestJson<APIResponse<ConfigSourceDocumentDTO>>(port,
    `/api/v1/config/sources/${encodeURIComponent(sourceKey)}${query}`, {
      headers: gatewayWorkspaceRequestHeaders(route), cache: "no-store", signal,
    }));
  assertDocumentTarget(document, route, sourceKey, idempotencyKey);
  return document;
}

/** 同一意图原样预检和保存；不在客户端合并来源或更新 active。 */
export async function preflightConfigSource(port: number, target: GatewayWorkspaceRequestTarget, intent: ConfigSourceEditRequest,
  signal?: AbortSignal, modelRef?: ModelReferenceDTO): Promise<ConfigSourcePreflightDTO> {
  const route = { ...target };
  if (intent.gateway_id !== route.gateway_id || intent.workspace_id !== route.workspace_id) throw new Error("配置编辑意图不属于原 Gateway／工作区");
  const { source_key: sourceKey } = intent;
  const query = modelRef ? `?${new URLSearchParams({ provider_id: modelRef.provider_id, model_id: modelRef.model_id })}` : '';
  const result = unwrapApiData(await requestJson<APIResponse<ConfigSourcePreflightDTO>>(port,
    `/api/v1/config/sources/${encodeURIComponent(sourceKey)}/preflight${query}`, {
      method: "POST", headers: gatewayWorkspaceRequestHeaders(route), body: JSON.stringify(intent), cache: "no-store", signal,
    }));
  if (!result.document) throw new Error("配置预检没有返回原来源完整文档");
  assertDocumentTarget(result.document, route, sourceKey);
  if (modelRef && (result.model_options?.model_ref?.provider_id !== modelRef.provider_id
      || result.model_options.model_ref.model_id !== modelRef.model_id)) throw new Error("模型选项回执不属于原模型");
  return result;
}

export async function saveConfigSource(port: number, target: GatewayWorkspaceRequestTarget, intent: ConfigSourceEditRequest,
  signal?: AbortSignal): Promise<ConfigSourceDocumentDTO> {
  const route = { ...target };
  if (intent.gateway_id !== route.gateway_id || intent.workspace_id !== route.workspace_id) throw new Error("配置编辑意图不属于原 Gateway／工作区");
  const { source_key: sourceKey, idempotency_key: key } = intent;
  const document = unwrapApiData(await requestJson<APIResponse<ConfigSourceDocumentDTO>>(port,
    `/api/v1/config/sources/${encodeURIComponent(sourceKey)}`, {
      method: "PUT", headers: gatewayWorkspaceRequestHeaders(route), body: JSON.stringify(intent), cache: "no-store", signal,
    }));
  assertDocumentTarget(document, route, sourceKey, key);
  if (!document.edit) throw new Error("配置保存回执没有原提交记录，请用原幂等键查询");
  return document;
}

/** retry 只修改原 pending 状态；它不执行 Backend 重启或生成健康证明。 */
export async function retryConfigPending(port: number, target: GatewayWorkspaceRequestTarget, candidateRef: string,
  signal?: AbortSignal): Promise<ConfigReloadStatusDTO> {
  return unwrapApiData(await requestJson<APIResponse<ConfigReloadStatusDTO>>(port,
    `/api/v1/config/pending/retry?${new URLSearchParams({ candidate_ref: candidateRef })}`, {
      method: "POST", headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store", signal,
    }));
}

export async function discardConfigPending(port: number, target: GatewayWorkspaceRequestTarget, candidateRef: string,
  active: { active_revision: number; active_digest: string }, signal?: AbortSignal): Promise<ConfigReloadStatusDTO> {
  return unwrapApiData(await requestJson<APIResponse<ConfigReloadStatusDTO>>(port,
    `/api/v1/config/pending/discard?${new URLSearchParams({ candidate_ref: candidateRef })}`, {
      method: "POST", headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store", signal,
      body: JSON.stringify({ expected_active_revision: active.active_revision, expected_active_digest: active.active_digest }),
    }));
}
