import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { useMcpConnections } from "../../../hooks/mcpConnections/useMcpConnections";

const labels: Record<string, string> = {
  ready: "连接可用", disabled: "配置已禁用", authorization_required: "需要授权", failed: "失败",
  pending: "已排队", waiting_user: "等待用户授权", verifying: "正在验证连接与目录", succeeded: "操作已完成",
  expired: "已过期", cancelled: "已取消",
};
const active = new Set(["pending", "waiting_user", "verifying"]);

export default function McpConnections({ apiPort, target }: { apiPort: number; target: GatewayWorkspaceRequestTarget }) {
  const state = useMcpConnections(apiPort, target);
  return <div style={{ padding: 12, overflow: "auto" }} aria-label="工作区 MCP 连接">
    <p>MCP Server 连接属于当前工作区。上游应用授权请使用 Server 提供的管理能力。</p>
    <button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新 MCP 状态</button>
    {state.error ? <div role="alert">{state.error}</div> : null}
    {!state.loaded && !state.error ? <p role="status">正在读取 MCP 连接…</p> : null}
    {state.loaded && state.servers.length === 0 ? <p>当前工作区没有配置 MCP Server。</p> : null}
    {state.servers.map((server) => {
      const operation = state.operations.find((item) => item.server_id === server.server_id);
      const pending = operation && active.has(operation.status);
      return <section key={server.server_id} aria-label={`MCP Server ${server.server_id}`} style={{ padding: "10px 0", borderBottom: "1px solid var(--bt-panel-border)" }}>
        <strong>{server.server_id}</strong> · {server.transport} · <span>{labels[server.status] ?? server.status}</span>
        <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
          <button type="button" disabled={state.busy || !server.enabled || Boolean(pending)} onClick={() => void state.submit(server.server_id, "connect")}>连接</button>
          <button type="button" disabled={state.busy || server.status !== "ready"} onClick={() => void state.submit(server.server_id, "disconnect")}>断开</button>
          {operation?.status === "waiting_user" ? <button type="button" disabled={state.busy} onClick={() => void state.authorize(operation)}>打开授权页面</button> : null}
          {pending ? <button type="button" disabled={state.busy} onClick={() => void state.cancel(operation)}>取消连接操作</button> : null}
        </div>
        {operation ? <div role="status">
          {operation.action === "connect" ? "连接" : "断开"}：{labels[operation.status] ?? operation.status} · 阶段 {operation.stage}
          {operation.error_code ? <div role="alert">{operation.error_code}</div> : null}
          {pending ? <div>期限：{new Date(operation.deadline * 1000).toLocaleString()}</div> : null}
          {operation.status === "succeeded" && operation.action === "connect" ? <div>新工具在允许的上下文边界生效；原 Turn 的冻结目录保持原值。</div> : null}
        </div> : null}
      </section>;
    })}
  </div>;
}
