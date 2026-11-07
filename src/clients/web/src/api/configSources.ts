import type { APIResponse, ConfigSourcesDTO } from "../types/backend";
import type { ConfigReloadStatusDTO, ConfigSourceDocumentDTO, ConfigSourceEditRequest, ConfigSourcePreflightDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

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
  signal?: AbortSignal): Promise<ConfigSourcePreflightDTO> {
  const route = { ...target };
  if (intent.gateway_id !== route.gateway_id || intent.workspace_id !== route.workspace_id) throw new Error("配置编辑意图不属于原 Gateway／工作区");
  const { source_key: sourceKey } = intent;
  const result = unwrapApiData(await requestJson<APIResponse<ConfigSourcePreflightDTO>>(port,
    `/api/v1/config/sources/${encodeURIComponent(sourceKey)}/preflight`, {
      method: "POST", headers: gatewayWorkspaceRequestHeaders(route), body: JSON.stringify(intent), cache: "no-store", signal,
    }));
  if (!result.document) throw new Error("配置预检没有返回原来源完整文档");
  assertDocumentTarget(result.document, route, sourceKey);
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
