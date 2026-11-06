import type { APIResponse } from "../types/backend";
import type { ConfigSourceDocumentDTO } from "../types/protocol_generated/boxteam/workspace/v2/public";
import type { GatewayWorkspaceRequestTarget } from "./workspaceIdentity";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "./http";

/** 正文与 CAS 只消费原来源 owner；查询提交沿用原幂等键。 */
export async function getConfigSourceDocument(port: number, target: GatewayWorkspaceRequestTarget, sourceKey: string,
  { idempotencyKey, signal }: { idempotencyKey?: string; signal?: AbortSignal } = {}): Promise<ConfigSourceDocumentDTO> {
  const { gateway_id, workspace_id } = target;
  const query = idempotencyKey === undefined ? "" : `?${new URLSearchParams({ idempotency_key: idempotencyKey })}`;
  const document = unwrapApiData(await requestJson<APIResponse<ConfigSourceDocumentDTO>>(port,
    `/api/v1/config/sources/${encodeURIComponent(sourceKey)}${query}`, {
      headers: gatewayWorkspaceRequestHeaders(target), cache: "no-store", signal,
    }));
  if (document.gateway_id !== gateway_id || document.workspace_id !== workspace_id || document.source_key !== sourceKey
      || (idempotencyKey !== undefined && document.edit && document.edit.idempotency_key !== idempotencyKey)) {
    throw new Error("配置来源回执不属于原 Gateway／工作区、来源或提交");
  }
  return document;
}
