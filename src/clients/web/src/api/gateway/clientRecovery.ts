import type { APIResponse, GatewayClientRecoveryDTO } from "../../types/backend";
import { requestJson, unwrapApiData } from "../http";

/** 仅身份升级受阻时领取已完成迁移的冻结记录；普通请求不查询恢复材料。 */
export async function getGatewayClientRecovery(port: number): Promise<GatewayClientRecoveryDTO> {
  return unwrapApiData(await requestJson<APIResponse<GatewayClientRecoveryDTO>>(port, "/api/gateway/client-recovery"));
}
