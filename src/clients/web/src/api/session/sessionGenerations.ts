import type { APIResponse, SessionGenerationPreviewDTO, SessionGenerationPreviewRequest } from "../../types/backend";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "../http";
import type { GatewayWorkspaceRequestTarget } from "../workspaceIdentity";

export async function previewSessionGeneration(
  port: number,
  target: GatewayWorkspaceRequestTarget,
  payload: SessionGenerationPreviewRequest,
  signal: AbortSignal,
): Promise<SessionGenerationPreviewDTO> {
  return unwrapApiData(await requestJson<APIResponse<SessionGenerationPreviewDTO>>(
    port, "/api/v1/session-generations/preview-placement", {
      method: "POST", headers: gatewayWorkspaceRequestHeaders(target),
      body: JSON.stringify(payload), signal,
    },
  ));
}
