/**
 * 扩展窗口的入口识别与 URL 组装。
 *
 * 扩展窗口通过 `/extension` 路径或 `window=extension` 查询参数进入，
 * 窗口内要展示的资源（工作区、会话、资源）全部来自 URL 参数。
 * 本模块只做纯解析与纯拼接，不持有状态、不打开窗口。
 */

export type ExtensionResourceKind = "browser" | "terminal" | "debug";

export type ExtensionWindowRequest = {
  kind: ExtensionResourceKind | null;
  resourceId: string | null;
  workspaceId: string | null;
  sessionId: string | null;
};

export const EXTENSION_WINDOW_NAME = "boxteam-extension";

export function resolveExtensionWindowRequest(): ExtensionWindowRequest | null {
  if (typeof window === "undefined") {
    return null;
  }
  const params = new URLSearchParams(window.location.search);
  if (window.location.pathname !== "/extension" && params.get("window") !== "extension") {
    return null;
  }
  const browserId = params.get("browserId");
  const resourceId = params.get("resourceId") ?? browserId;
  const resourceType = params.get("resourceType") ?? (browserId ? "browser" : null);
  const kind = resourceType === "browser" || resourceType === "terminal" || resourceType === "debug"
    ? resourceType
    : null;
  return {
    kind,
    resourceId,
    workspaceId: params.get("workspaceId"),
    sessionId: params.get("sessionId"),
  };
}

export type ExtensionWindowTarget = {
  kind: ExtensionResourceKind;
  resourceId?: string;
  workspaceId?: string | null;
  sessionId?: string | null;
};

export function buildExtensionWindowUrl(target: ExtensionWindowTarget): string {
  const url = new URL(window.location.href);
  url.pathname = "/extension";
  url.search = "";
  url.hash = "";
  url.searchParams.set("resourceType", target.kind);
  if (target.resourceId) url.searchParams.set("resourceId", target.resourceId);
  if (target.workspaceId) {
    url.searchParams.set("workspaceId", target.workspaceId);
  }
  if (target.sessionId) {
    url.searchParams.set("sessionId", target.sessionId);
  }
  return url.toString();
}
