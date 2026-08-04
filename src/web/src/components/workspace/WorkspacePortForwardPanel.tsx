import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  createWorkspacePortForward,
  deleteWorkspacePortForward,
  listWorkspacePortForwards,
  reconnectWorkspacePortForward,
} from "../../gatewayApi";
import type {
  CreateGatewayPortForwardRequest,
  GatewayPortForward,
  GatewayPortForwardList,
  GatewayPortForwardProtocol,
  GatewayWorkspace,
} from "../../types/backend";
import { useWarmConfirm } from "../WarmConfirmProvider";

export interface WorkspacePortForwardApi {
  list(port: number, workspaceId: string): Promise<GatewayPortForwardList>;
  create(
    port: number,
    workspaceId: string,
    payload: CreateGatewayPortForwardRequest,
  ): Promise<GatewayPortForwardList>;
  remove(
    port: number,
    workspaceId: string,
    forwardId: string,
  ): Promise<GatewayPortForwardList>;
  reconnect(
    port: number,
    workspaceId: string,
    forwardId: string,
  ): Promise<GatewayPortForwardList>;
}

const defaultApi: WorkspacePortForwardApi = {
  list: listWorkspacePortForwards,
  create: createWorkspacePortForward,
  remove: deleteWorkspacePortForward,
  reconnect: reconnectWorkspacePortForward,
};

const STATUS_LABELS: Record<GatewayPortForward["status"], string> = {
  starting: "正在连接",
  active: "已转发",
  error: "连接失败",
  stopped: "已停止",
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parsePort(value: string, label: string): number {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${label}必须是 1–65535 之间的整数`);
  }
  return port;
}

function isValidPortInput(value: string): boolean {
  const port = Number(value);
  return Number.isInteger(port) && port >= 1 && port <= 65535;
}

export default function WorkspacePortForwardPanel({
  apiPort,
  workspace,
  active,
  api = defaultApi,
  confirmStop,
}: {
  apiPort: number;
  workspace: GatewayWorkspace | null;
  active: boolean;
  api?: WorkspacePortForwardApi;
  confirmStop?: (forward: GatewayPortForward) => Promise<boolean>;
}) {
  const confirm = useWarmConfirm();
  const [forwards, setForwards] = useState<GatewayPortForwardList | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  const [reconnectingId, setReconnectingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [remotePort, setRemotePort] = useState("");
  const [localPort, setLocalPort] = useState("");
  const [protocol, setProtocol] = useState<GatewayPortForwardProtocol>("http");
  const [label, setLabel] = useState("");
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const requestRevisionRef = useRef(0);
  const currentWorkspaceIdRef = useRef(workspace?.workspace_id ?? null);
  currentWorkspaceIdRef.current = workspace?.workspace_id ?? null;

  const refresh = useCallback(async () => {
    if (!workspace || workspace.connection_kind !== "remote_gateway" || !active) {
      return;
    }
    const workspaceId = workspace.workspace_id;
    const revision = ++requestRevisionRef.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.list(apiPort, workspaceId);
      if (
        requestRevisionRef.current === revision
        && currentWorkspaceIdRef.current === workspaceId
      ) {
        setForwards(result);
      }
    } catch (loadError) {
      if (
        requestRevisionRef.current === revision
        && currentWorkspaceIdRef.current === workspaceId
      ) {
        setError(`刷新端口转发失败：${errorMessage(loadError)}`);
      }
    } finally {
      if (requestRevisionRef.current === revision) {
        setLoading(false);
      }
    }
  }, [active, api, apiPort, workspace]);

  useEffect(() => {
    requestRevisionRef.current += 1;
    setForwards(null);
    setError(null);
    setSubmitting(false);
    setStoppingId(null);
    setReconnectingId(null);
    setIsCreateOpen(false);
    setRemotePort("");
    setLocalPort("");
    setProtocol("http");
    setLabel("");
    if (active && workspace?.connection_kind === "remote_gateway") {
      void refresh();
    }
  }, [active, refresh, workspace?.connection_kind, workspace?.workspace_id]);

  const recoverAfterFailure = async (workspaceId: string, message: string) => {
    setError(message);
    try {
      const recovered = await api.list(apiPort, workspaceId);
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(recovered);
      }
    } catch (recoverError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setError(`${message}；重新读取状态也失败：${errorMessage(recoverError)}`);
      }
    }
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workspace || workspace.connection_kind !== "remote_gateway") {
      return;
    }
    let payload: CreateGatewayPortForwardRequest;
    try {
      payload = {
        remote_port: parsePort(remotePort, "远端端口"),
        local_port: localPort ? parsePort(localPort, "本地端口") : null,
        protocol,
        label: label.trim() || null,
      };
    } catch (validationError) {
      setError(errorMessage(validationError));
      return;
    }

    const workspaceId = workspace.workspace_id;
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.create(apiPort, workspaceId, payload);
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
        setRemotePort("");
        setLocalPort("");
        setLabel("");
        setIsCreateOpen(false);
      }
    } catch (createError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(
          workspaceId,
          `创建端口转发失败：${errorMessage(createError)}`,
        );
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setSubmitting(false);
      }
    }
  };

  const handleStop = async (forward: GatewayPortForward) => {
    if (!workspace) return;
    const accepted = confirmStop
      ? await confirmStop(forward)
      : await confirm({
          title: "停止端口转发",
          message: `停止 ${forward.label || `远端端口 ${forward.remote_port}`}？本机 ${forward.local_host}:${forward.local_port} 将立即不可访问。`,
          confirmText: "停止转发",
          danger: true,
        });
    if (!accepted) return;

    const workspaceId = workspace.workspace_id;
    setStoppingId(forward.forward_id);
    setError(null);
    try {
      const result = await api.remove(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
      }
    } catch (stopError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(
          workspaceId,
          `停止端口转发失败：${errorMessage(stopError)}`,
        );
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setStoppingId(null);
      }
    }
  };

  const handleReconnect = async (forward: GatewayPortForward) => {
    if (!workspace) return;
    const workspaceId = workspace.workspace_id;
    setReconnectingId(forward.forward_id);
    setError(null);
    try {
      const result = await api.reconnect(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
      }
    } catch (reconnectError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(
          workspaceId,
          `重新连接失败：${errorMessage(reconnectError)}`,
        );
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setReconnectingId(null);
      }
    }
  };

  if (!workspace) {
    return (
      <section className="port-forward-panel port-forward-panel-empty">
        <h3>SSH 端口转发</h3>
        <p>打开一个工作区会话后，可在这里管理该工作区的开发服务端口。</p>
      </section>
    );
  }

  if (workspace.connection_kind !== "remote_gateway") {
    return (
      <section className="port-forward-panel port-forward-panel-empty">
        <header className="port-forward-header">
          <div>
            <span className="port-forward-scope">工作区资源</span>
            <h3>端口转发</h3>
            <p title={workspace.root_path}>{workspace.name} · 本地工作区</p>
          </div>
        </header>
        <p>当前是本地工作区，开发服务可直接通过本机端口访问，无需 SSH 转发。</p>
      </section>
    );
  }

  const forwardCount = forwards?.items.length ?? 0;
  const remoteIdentity = workspace.remote
    ? `${workspace.remote.ssh_config_host || workspace.remote.name} · ${workspace.remote.username}@${workspace.remote.host}:${workspace.remote.port}`
    : "远程 Gateway";
  const createDisabled = submitting
    || !isValidPortInput(remotePort)
    || (localPort !== "" && !isValidPortInput(localPort));

  return (
    <section className="port-forward-panel" aria-label="工作区 SSH 端口转发">
      <header className="port-forward-header">
        <div className="port-forward-heading">
          <span className="port-forward-scope">工作区资源</span>
          <div className="port-forward-title-line">
            <h3>端口转发</h3>
            <span className="port-forward-count" aria-label={`${forwardCount} 个端口`}>
              {loading && !forwards ? "…" : forwardCount}
            </span>
          </div>
        </div>
        <div className="port-forward-header-actions">
          <button
            className="port-forward-add-toggle"
            type="button"
            aria-expanded={isCreateOpen}
            aria-controls="workspace-port-forward-form"
            onClick={() => setIsCreateOpen((open) => !open)}
            disabled={submitting}
          >
            {isCreateOpen ? "取消" : "新增转发"}
          </button>
          <button
            className="port-forward-refresh"
            type="button"
            aria-label={loading ? "正在刷新端口转发" : "刷新端口转发"}
            title={loading ? "正在刷新" : "刷新"}
            onClick={() => void refresh()}
            disabled={loading || submitting}
          >
            <span aria-hidden="true">↻</span>
          </button>
        </div>
        <p className="port-forward-context" title={`${workspace.name} · ${remoteIdentity}`}>
          {workspace.name} · {remoteIdentity}
        </p>
      </header>

      {isCreateOpen ? (
        <div className="port-forward-create-region">
          <p className="port-forward-guidance">
            转发此工作区的远端端口，仅监听本机 127.0.0.1。
          </p>
          <form
            id="workspace-port-forward-form"
            className="port-forward-form"
            onSubmit={(event) => void handleCreate(event)}
          >
            <label className="port-forward-field port-forward-field-primary">
              <span>远端端口</span>
              <input
                type="number"
                min="1"
                max="65535"
                required
                inputMode="numeric"
                placeholder="例如 5173"
                value={remotePort}
                onChange={(event) => setRemotePort(event.target.value)}
                disabled={submitting}
              />
            </label>
            <label className="port-forward-field">
              <span>本地端口（可选）</span>
              <input
                type="number"
                min="1"
                max="65535"
                inputMode="numeric"
                placeholder="自动分配"
                value={localPort}
                onChange={(event) => setLocalPort(event.target.value)}
                disabled={submitting}
              />
            </label>
            <label className="port-forward-field">
              <span>协议</span>
              <select
                value={protocol}
                onChange={(event) => setProtocol(event.target.value as GatewayPortForwardProtocol)}
                disabled={submitting}
              >
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="tcp">TCP</option>
              </select>
            </label>
            <label className="port-forward-field">
              <span>名称（可选）</span>
              <input
                type="text"
                maxLength={120}
                placeholder="例如 Vite 开发服务器"
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                disabled={submitting}
              />
            </label>
            <button className="port-forward-create" type="submit" disabled={createDisabled}>
              {submitting ? "正在创建 SSH 转发…" : "创建转发"}
            </button>
          </form>
        </div>
      ) : null}

      {error ? (
        <div className="port-forward-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => void refresh()} disabled={loading}>重试刷新</button>
        </div>
      ) : null}

      {!isCreateOpen || error ? <div className="port-forward-list" aria-live="polite">
        {loading && !forwards ? (
          <p className="port-forward-empty">正在读取端口…</p>
        ) : null}
        {!loading && forwards?.items.length === 0 ? (
          <p className="port-forward-empty">暂无转发。点击“新增转发”连接开发服务。</p>
        ) : null}
        {forwards?.items.map((forward) => {
          const canOpen = forward.status === "active"
            && forward.protocol !== "tcp"
            && Boolean(forward.local_url);
          return (
            <article
              className={`port-forward-card is-${forward.status}`}
              key={forward.forward_id}
              aria-label={`${forward.label || `端口 ${forward.remote_port}`}，状态${STATUS_LABELS[forward.status]}`}
            >
              <div className="port-forward-card-main">
                <div className="port-forward-card-title">
                  <code title={`${forward.remote_host}:${forward.remote_port} → ${forward.local_host}:${forward.local_port}`}>
                    {forward.remote_port}
                    <span aria-hidden="true"> → </span>
                    {forward.local_port}
                  </code>
                  {forward.status !== "active" ? (
                    <span className="port-forward-status">{STATUS_LABELS[forward.status]}</span>
                  ) : null}
                </div>
                <span className="port-forward-card-label" title={forward.label || undefined}>
                  {forward.label || forward.protocol.toUpperCase()}
                </span>
                {forward.error ? <p className="port-forward-item-error">{forward.error}</p> : null}
              </div>
              <div className="port-forward-card-actions">
                {canOpen ? (
                  <a href={forward.local_url!} target="_blank" rel="noreferrer">打开</a>
                ) : null}
                {forward.status === "error" || forward.status === "stopped" ? (
                  <button
                    type="button"
                    disabled={reconnectingId === forward.forward_id}
                    onClick={() => void handleReconnect(forward)}
                  >
                    {reconnectingId === forward.forward_id ? "连接中…" : "重连"}
                  </button>
                ) : null}
                {forward.status !== "stopped" ? (
                  <button
                    type="button"
                    className="danger-soft"
                    disabled={stoppingId === forward.forward_id}
                    onClick={() => void handleStop(forward)}
                  >
                    {stoppingId === forward.forward_id ? "停止中…" : "停止"}
                  </button>
                ) : null}
              </div>
            </article>
          );
        })}
      </div> : null}
    </section>
  );
}
