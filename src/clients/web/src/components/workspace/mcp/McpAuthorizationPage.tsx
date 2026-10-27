import { useEffect, useState } from "react";
import { DEFAULT_BACKEND_PORT } from "../../../api/http";
import { requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { listGatewayWorkspaces } from "../../../gatewayApi";
import { errorMessage } from "../../../utils/errorMessage";
import McpConnections from "./McpConnections";

/** 会话中的非敏感管理入口；始终先解析实际 Gateway 注册目标。 */
export default function McpAuthorizationPage({ workspaceId }: { workspaceId: string }) {
  const [target, setTarget] = useState<GatewayWorkspaceRequestTarget | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void listGatewayWorkspaces(DEFAULT_BACKEND_PORT).then((result) => {
      const target = requireGatewayWorkspaceRequestTarget(result.items, workspaceId);
      if (active) setTarget(target);
    }).catch((cause: unknown) => { if (active) setError(errorMessage(cause)); });
    return () => { active = false; };
  }, [workspaceId]);
  return <main>
    <h1>工作区 MCP 连接与授权</h1>
    <a href="/">返回主窗口</a>
    {error ? <div role="alert">{error}</div> : null}
    {!target && !error ? <p role="status">正在解析工作区目标…</p> : null}
    {target ? <McpConnections key={workspaceId} apiPort={DEFAULT_BACKEND_PORT} target={target} /> : null}
  </main>;
}
