import type { ResourceThreadRef } from "../api/workspaceResources";

export type AttachResourceKind = "terminal" | "browser";

export function buildGatewayAttachUrl(
  kind: AttachResourceKind,
  workspaceId: string,
  resourceId: string,
  embedded = false,
  thread?: ResourceThreadRef,
): string {
  if (!workspaceId) {
    throw new Error("打开后台连接需要 workspace_id");
  }
  if (!resourceId) {
    throw new Error("打开后台连接需要 resource_id");
  }
  const url = new URL(`/api/gateway/attach/${kind}/`, window.location.origin);
  url.searchParams.set("workspaceId", workspaceId);
  url.searchParams.set(kind === "terminal" ? "terminalId" : "browserId", resourceId);
  if (thread) {
    url.searchParams.set("sessionId", thread.session_id);
    url.searchParams.set("threadId", thread.thread_id);
  }
  if (embedded) {
    url.searchParams.set("embedded", "1");
  }
  return url.toString();
}
