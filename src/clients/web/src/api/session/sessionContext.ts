import type { APIResponse } from "../../types/backend";
import type { SessionContextReadResultDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { requestJson, unwrapApiData, workspaceHeader } from "../http";

export function sessionContextResource(sessionId: string, assemblyId?: string): string {
  if (!sessionId) throw new Error("上下文检查缺少 session owner");
  return `boxteam://session/${sessionId}${assemblyId ? `#assembly=${assemblyId}` : ""}`;
}

export async function readSessionContext(
  port: number,
  workspaceId: string,
  resource: string,
  view: "assembly" | "assemblies",
  options: { signal: AbortSignal; cursor?: string | null; revision?: string | null },
): Promise<SessionContextReadResultDTO> {
  if (!workspaceId) throw new Error("上下文检查缺少 workspace owner");
  const response = await requestJson<APIResponse<SessionContextReadResultDTO>>(
    port, "/api/v1/context/read", {
      method: "POST",
      headers: { ...workspaceHeader(workspaceId), "Content-Type": "application/json" },
      signal: options.signal,
      body: JSON.stringify({ resource, view, include: ["visible_text", "reasoning", "tool_summary"],
        limit: view === "assemblies" ? 2 : 4, max_chars: 16000,
        cursor: options.cursor, expected_revision: options.revision }),
    },
  );
  const page = unwrapApiData(response);
  if (page.view !== view) throw new Error("上下文响应 view 不匹配");
  return page;
}
