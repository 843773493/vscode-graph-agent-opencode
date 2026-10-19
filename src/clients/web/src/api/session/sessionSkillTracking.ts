import type { APIResponse, SessionSkillUntrackResult } from "../../types/backend";
import { gatewayWorkspaceRequestHeaders, requestJson, unwrapApiData } from "../http";
import type { GatewayWorkspaceRequestTarget } from "../workspaceIdentity";

export async function untrackSessionSkill(
  port: number,
  workspaceTarget: GatewayWorkspaceRequestTarget,
  sessionId: string,
  threadId: string,
  name: string,
): Promise<SessionSkillUntrackResult> {
  const result = unwrapApiData(await requestJson<APIResponse<SessionSkillUntrackResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}/skills/untrack`,
    { method: "POST", headers: gatewayWorkspaceRequestHeaders(workspaceTarget), body: JSON.stringify({ name }) },
  ));
  if (result.session_id !== sessionId || result.thread_id !== threadId || result.name !== name || result.mode !== "untrack") {
    throw new Error("停止跟踪 Skill 的响应与请求归属不一致");
  }
  return result;
}
