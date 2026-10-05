import type { APIResponse, Session } from "../../types/backend";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { requestJson, unwrapApiData, workspaceHeader } from "../http";

export type SessionContextSession = Pick<
  Session,
  "session_id" | "resource_identity" | "uri" | "scope"
>;

export async function readSessionContext(
  port: number,
  gatewayWorkspaceId: string,
  session: SessionContextSession,
  view: "assembly" | "assemblies",
  options: {
    signal: AbortSignal;
    cursor?: string | null;
    revision?: string | null;
    assemblyRef?: string | null;
  },
): Promise<SessionContextReadResultDTO> {
  if (!gatewayWorkspaceId) throw new Error("上下文检查缺少 Gateway workspace 路由 ID");
  if (!session.session_id || !session.resource_identity || !session.uri || !session.scope) {
    throw new Error("Session API 响应缺少上下文资源身份");
  }
  if (view === "assembly" && !options.assemblyRef) throw new Error("assembly view 缺少 assembly_ref");
  const response = await requestJson<APIResponse<SessionContextReadResultDTO>>(
    port, "/api/v1/context/read", {
      method: "POST",
      headers: { ...workspaceHeader(gatewayWorkspaceId), "Content-Type": "application/json" },
      signal: options.signal,
      body: JSON.stringify({
        resource_identity: session.resource_identity,
        uri: session.uri,
        scope: session.scope,
        view,
        assembly_ref: options.assemblyRef,
        include: ["visible_text", "reasoning", "tool_summary"],
        limit: view === "assemblies" ? 2 : 4,
        max_chars: 16000,
        cursor: options.cursor,
        revision: options.revision,
      }),
    },
  );
  const page = unwrapApiData(response);
  if (page.view !== view) throw new Error("上下文响应 view 不匹配");
  if (
    page.resource_identity !== session.resource_identity
    || page.uri !== session.uri
    || page.scope !== session.scope
  ) {
    throw new Error("上下文响应资源身份与 Session API 不一致");
  }
  if (view === "assembly" && page.assembly_ref !== options.assemblyRef) {
    throw new Error("冻结 assembly 响应引用与请求不一致");
  }
  return page;
}
