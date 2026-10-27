import { completeMcpAuthorization, getMcpAuthorization, type McpConnectionOperationDTO } from "../../api/mcpConnections";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";

export const MCP_OAUTH_CALLBACK_PATH = "/mcp-oauth-callback.html";
export const MCP_OAUTH_MESSAGE = "boxteam-mcp-oauth-callback:v1";

/** 凭据仅由指定同源回调窗口交给原目标；实际 state/PKCE 校验仍归 SDK。 */
export async function authorizeMcpInWindow(port: number, target: GatewayWorkspaceRequestTarget, operation: McpConnectionOperationDTO, signal: AbortSignal) {
  const popup = window.open("about:blank", "_blank", "popup,width=720,height=800");
  if (!popup) throw new Error("浏览器阻止了授权窗口，请允许弹出窗口后重试。");
  try {
    const { authorization_url: authorizationUrl } = await getMcpAuthorization(port, target, operation.operation_id, signal);
    const url = new URL(authorizationUrl);
    const redirectUri = url.searchParams.get("redirect_uri");
    const redirect = redirectUri ? new URL(redirectUri) : null;
    if (!redirect || redirect.origin !== window.location.origin || redirect.pathname !== MCP_OAUTH_CALLBACK_PATH || redirect.search || redirect.hash) {
      throw new Error(`此 Web 入口要求该 Server 的 oauth.redirect_uri 配置为 ${window.location.origin}${MCP_OAUTH_CALLBACK_PATH}，以便将回调交给原工作区。`);
    }
    if (signal.aborted) throw signal.reason;
    return await new Promise<McpConnectionOperationDTO>((resolve, reject) => {
      let received = false;
      let settled = false;
      const finish = (error?: unknown, value?: McpConnectionOperationDTO) => {
        if (settled) return;
        settled = true;
        window.removeEventListener("message", receive);
        signal.removeEventListener("abort", abort);
        window.clearInterval(closed);
        window.clearTimeout(expiry);
        popup.close();
        if (value !== undefined) resolve(value); else reject(error);
      };
      const abort = () => finish(signal.reason);
      const receive = (event: MessageEvent<unknown>) => {
        if (event.origin !== window.location.origin || event.source !== popup || received) return;
        const data = event.data;
        if (!data || typeof data !== "object" || !("type" in data) || data.type !== MCP_OAUTH_MESSAGE) return;
        received = true;
        if (!("code" in data) || typeof data.code !== "string" || !("state" in data) || typeof data.state !== "string") {
          finish(new Error("授权已拒绝或回调缺少 code/state；连接尚未就绪。"));
          return;
        }
        void completeMcpAuthorization(port, target, operation.operation_id, { code: data.code, state: data.state }, signal)
          .then((value) => finish(undefined, value), (error: unknown) => finish(error));
      };
      const closed = window.setInterval(() => {
        if (popup.closed && !received) finish(new Error("授权窗口已关闭，连接状态尚未确认；可以重试授权或查看实际状态。"));
      }, 500);
      const expiry = window.setTimeout(() => finish(new Error("原授权操作已到期限，请刷新实际状态。")), Math.max(0, operation.deadline * 1000 - Date.now()));
      window.addEventListener("message", receive);
      signal.addEventListener("abort", abort, { once: true });
      popup.location.href = authorizationUrl;
    });
  } finally {
    popup.close();
  }
}
