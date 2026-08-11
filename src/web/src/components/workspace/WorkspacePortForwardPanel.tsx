import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  changeWorkspacePortForwardLabel,
  changeWorkspacePortForwardLocalPort,
  createWorkspacePortForward,
  deleteWorkspacePortForward,
  listWorkspacePortForwards,
  reconnectWorkspacePortForward,
} from "../../gatewayApi";
import type {
  ChangeGatewayPortForwardLabelRequest,
  ChangeGatewayPortForwardLocalPortRequest,
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
  changeLocalPort(
    port: number,
    workspaceId: string,
    forwardId: string,
    payload: ChangeGatewayPortForwardLocalPortRequest,
  ): Promise<GatewayPortForwardList>;
  changeLabel?: (
    port: number,
    workspaceId: string,
    forwardId: string,
    payload: ChangeGatewayPortForwardLabelRequest,
  ) => Promise<GatewayPortForwardList>;
}

const defaultApi: WorkspacePortForwardApi = {
  list: listWorkspacePortForwards,
  create: createWorkspacePortForward,
  remove: deleteWorkspacePortForward,
  reconnect: reconnectWorkspacePortForward,
  changeLocalPort: changeWorkspacePortForwardLocalPort,
  changeLabel: changeWorkspacePortForwardLabel,
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

function portLabel(forward: GatewayPortForward): string {
  return forward.label
    ? `${forward.label} (${forward.remote_port})`
    : String(forward.remote_port);
}

function localAddress(forward: GatewayPortForward): string {
  return forward.local_url
    ? forward.local_url.replace(/^https?:\/\//, "")
    : `${forward.local_host}:${forward.local_port}`;
}

function errorSummary(error: string): string {
  const firstClause = error.split("；", 1)[0]?.trim() || error.trim();
  return firstClause.length > 140 ? `${firstClause.slice(0, 137)}…` : firstClause;
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
  const [changingLocalPortId, setChangingLocalPortId] = useState<string | null>(null);
  const [changingLabelId, setChangingLabelId] = useState<string | null>(null);
  const [editingLocalPortId, setEditingLocalPortId] = useState<string | null>(null);
  const [editingLocalPort, setEditingLocalPort] = useState("");
  const [editingLabelId, setEditingLabelId] = useState<string | null>(null);
  const [editingLabel, setEditingLabel] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copyRetryId, setCopyRetryId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [contextMenuId, setContextMenuId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [remotePort, setRemotePort] = useState("");
  const [localPort, setLocalPort] = useState("");
  const [protocol, setProtocol] = useState<GatewayPortForwardProtocol>("http");
  const [label, setLabel] = useState("");
  const [filterText, setFilterText] = useState("");
  const [isFilterOpen, setIsFilterOpen] = useState(false);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [isTableMenuOpen, setIsTableMenuOpen] = useState(false);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const requestRevisionRef = useRef(0);
  const currentWorkspaceIdRef = useRef(workspace?.workspace_id ?? null);
  currentWorkspaceIdRef.current = workspace?.workspace_id ?? null;

  const refresh = useCallback(async () => {
    if (!workspace || workspace.connection_kind !== "remote_gateway" || !active) return;
    const workspaceId = workspace.workspace_id;
    const revision = ++requestRevisionRef.current;
    setLoading(true);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.list(apiPort, workspaceId);
      if (requestRevisionRef.current === revision && currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
      }
    } catch (loadError) {
      if (requestRevisionRef.current === revision && currentWorkspaceIdRef.current === workspaceId) {
        setError(`刷新端口转发失败：${errorMessage(loadError)}`);
      }
    } finally {
      if (requestRevisionRef.current === revision) setLoading(false);
    }
  }, [active, api, apiPort, workspace]);

  useEffect(() => {
    requestRevisionRef.current += 1;
    setForwards(null);
    setError(null);
    setSubmitting(false);
    setStoppingId(null);
    setReconnectingId(null);
    setChangingLocalPortId(null);
    setChangingLabelId(null);
    setEditingLocalPortId(null);
    setEditingLocalPort("");
    setEditingLabelId(null);
    setEditingLabel("");
    setCopiedId(null);
    setCopyRetryId(null);
    setFocusedId(null);
    setContextMenuId(null);
    setIsCreateOpen(false);
    setRemotePort("");
    setLocalPort("");
    setProtocol("http");
    setLabel("");
    setFilterText("");
    setIsFilterOpen(false);
    setIsTableMenuOpen(false);
    if (active && workspace?.connection_kind === "remote_gateway") void refresh();
  }, [active, refresh, workspace?.connection_kind, workspace?.workspace_id]);

  const recoverAfterFailure = async (workspaceId: string, message: string) => {
    setError(message);
    try {
      const recovered = await api.list(apiPort, workspaceId);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(recovered);
    } catch (recoverError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setError(`${message}；重新读取状态也失败：${errorMessage(recoverError)}`);
      }
    }
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workspace || workspace.connection_kind !== "remote_gateway") return;
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
    setCopyRetryId(null);
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
        await recoverAfterFailure(workspaceId, `创建端口转发失败：${errorMessage(createError)}`);
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setSubmitting(false);
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
    setCopyRetryId(null);
    try {
      const result = await api.remove(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(result);
    } catch (stopError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, `停止端口转发失败：${errorMessage(stopError)}`);
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setStoppingId(null);
    }
  };

  const handleCopyAddress = async (forward: GatewayPortForward) => {
    if (!forward.local_url) return;
    try {
      if (!navigator.clipboard) throw new Error("当前页面不允许访问剪贴板");
      await navigator.clipboard.writeText(forward.local_url);
      setCopiedId(forward.forward_id);
      setCopyRetryId(null);
      window.setTimeout(() => {
        setCopiedId((current) => current === forward.forward_id ? null : current);
      }, 1400);
    } catch (copyError) {
      setCopyRetryId(forward.forward_id);
      setError(`复制本地地址失败：${errorMessage(copyError)}；可直接选中转发地址手动复制`);
    }
  };

  const handleOpen = (forward: GatewayPortForward, mode: "browser" | "preview") => {
    if (!forward.local_url) return;
    const target = mode === "preview" ? "boxteam-port-preview" : "_blank";
    const opened = window.open(forward.local_url, target, "noopener,noreferrer");
    if (!opened) setError("无法打开端口页面：浏览器阻止了弹出窗口");
  };

  const handleReconnect = async (forward: GatewayPortForward) => {
    if (!workspace) return;
    const workspaceId = workspace.workspace_id;
    setReconnectingId(forward.forward_id);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.reconnect(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(result);
    } catch (reconnectError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, `重新连接失败：${errorMessage(reconnectError)}`);
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setReconnectingId(null);
    }
  };

  const handleChangeLocalPort = async (event: FormEvent<HTMLFormElement>, forward: GatewayPortForward) => {
    event.preventDefault();
    if (!workspace) return;
    let payload: ChangeGatewayPortForwardLocalPortRequest;
    try {
      payload = { local_port: parsePort(editingLocalPort, "本地端口") };
    } catch (validationError) {
      setError(errorMessage(validationError));
      return;
    }
    const workspaceId = workspace.workspace_id;
    setChangingLocalPortId(forward.forward_id);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.changeLocalPort(apiPort, workspaceId, forward.forward_id, payload);
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
        setEditingLocalPortId(null);
        setEditingLocalPort("");
      }
    } catch (changeError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, `更改本地端口失败：${errorMessage(changeError)}`);
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setChangingLocalPortId(null);
    }
  };

  const beginLabelEdit = (forward: GatewayPortForward) => {
    setEditingLabelId(forward.forward_id);
    setEditingLabel(forward.label ?? "");
    setError(null);
  };

  const handleChangeLabel = async (event: FormEvent<HTMLFormElement>, forward: GatewayPortForward) => {
    event.preventDefault();
    if (!workspace || !api.changeLabel) return;
    const workspaceId = workspace.workspace_id;
    setChangingLabelId(forward.forward_id);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.changeLabel(apiPort, workspaceId, forward.forward_id, {
        label: editingLabel.trim() || null,
      });
      if (currentWorkspaceIdRef.current === workspaceId) {
        setForwards(result);
        setEditingLabelId(null);
        setEditingLabel("");
      }
    } catch (changeError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, `更改端口标签失败：${errorMessage(changeError)}`);
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setChangingLabelId(null);
    }
  };

  useEffect(() => {
    if (!contextMenuId && !isTableMenuOpen) return;
    if (typeof document === "undefined") return;
    const closeContextMenu = () => {
      setContextMenuId(null);
      setIsTableMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeContextMenu);
    return () => document.removeEventListener("pointerdown", closeContextMenu);
  }, [contextMenuId, isTableMenuOpen]);

  useEffect(() => {
    if (isFilterOpen) filterInputRef.current?.focus();
  }, [isFilterOpen]);

  if (!workspace) {
    return (
      <section className="port-forward-panel port-forward-panel-empty">
        <p>打开一个工作区会话后，可在这里管理该工作区的开发服务端口。</p>
      </section>
    );
  }

  if (workspace.connection_kind !== "remote_gateway") {
    return (
      <section className="port-forward-panel port-forward-panel-empty" aria-label="工作区端口">
        <p className="port-forward-local-empty">当前是本地工作区，开发服务可直接通过本机端口访问，无需 SSH 转发。</p>
      </section>
    );
  }

  const normalizedFilter = filterText.trim().toLocaleLowerCase();
  const filteredForwards = forwards?.items.filter((forward) => [
    forward.remote_port,
    forward.local_port,
    forward.label,
    forward.protocol,
    forward.local_url,
    forward.status,
  ].some((value) => String(value ?? "").toLocaleLowerCase().includes(normalizedFilter))) ?? [];
  const createDisabled = submitting || !isValidPortInput(remotePort) || (localPort !== "" && !isValidPortInput(localPort));

  return (
    <section className="port-forward-panel" aria-label="工作区 SSH 端口转发">
      {error ? (
        <div className="port-forward-error" role="alert">
          <span>{error}</span>
          {copyRetryId ? (
            <button
              type="button"
              onClick={() => {
                const retryForward = forwards?.items.find((item) => item.forward_id === copyRetryId);
                if (retryForward) void handleCopyAddress(retryForward);
              }}
              disabled={loading}
            >
              重试复制
            </button>
          ) : (
            <button type="button" onClick={() => void refresh()} disabled={loading}>重试刷新</button>
          )}
        </div>
      ) : null}

      <div className="port-forward-list" aria-live="polite">
        {loading && !forwards ? <p className="port-forward-empty">正在读取端口…</p> : null}
        {!loading && forwards?.items.length === 0 ? (
          <p className="port-forward-empty">
            暂无转发。
            <button
              type="button"
              className="port-forward-empty-action"
              aria-expanded={isCreateOpen}
              aria-controls="workspace-port-forward-form"
              onClick={() => setIsCreateOpen((open) => !open)}
              disabled={submitting}
            >
              {isCreateOpen ? "取消" : "新增端口"}
            </button>
          </p>
        ) : null}
        {!loading && forwards && forwards.items.length > 0 && filteredForwards.length === 0 ? <p className="port-forward-empty">没有匹配的端口。</p> : null}
        {isFilterOpen ? (
          <label className="port-forward-inline-filter" aria-label="筛选端口">
            <span className="codicon codicon-search" aria-hidden="true" />
            <input
              ref={filterInputRef}
              type="search"
              placeholder="筛选端口..."
              value={filterText}
              onChange={(event) => setFilterText(event.target.value)}
            />
            <button type="button" aria-label="关闭端口筛选" onClick={() => { setIsFilterOpen(false); setFilterText(""); }}>
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </label>
        ) : null}
        {filteredForwards.length > 0 ? (
          <>
            <p className="port-forward-narrow-hint">左右滚动查看所有列和操作</p>
            <div
              className="port-forward-table ports-view"
              role="table"
              aria-label="端口转发表"
              onContextMenu={(event) => {
                event.preventDefault();
                setContextMenuId(null);
                setIsTableMenuOpen(true);
              }}
            >
            <div className="port-forward-table-header" role="row">
              <span role="columnheader" aria-label="端口状态" />
              <span role="columnheader">端口</span>
              <span role="columnheader">转发地址</span>
              <span role="columnheader">正在运行的进程</span>
              <span role="columnheader">源</span>
            </div>
            {filteredForwards.map((forward) => {
              const canOpen = forward.status === "active" && forward.protocol !== "tcp" && Boolean(forward.local_url);
              const isFocused = focusedId === forward.forward_id;
              const processDescription = forward.status === "error" ? "流程信息不可用" : "";
              return (
                <article
                  className={`port-forward-row is-${forward.status}${isFocused ? " is-focused" : ""}`}
                  key={forward.forward_id}
                  role="row"
                  tabIndex={0}
                  aria-label={`${portLabel(forward)}，状态${STATUS_LABELS[forward.status]}`}
                  onFocus={() => setFocusedId(forward.forward_id)}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    setFocusedId(forward.forward_id);
                    setContextMenuId(forward.forward_id);
                    setIsTableMenuOpen(false);
                  }}
                  onMouseLeave={(event) => {
                    const activeElement = document.activeElement;
                    if (activeElement !== event.currentTarget && !event.currentTarget.contains(activeElement)) {
                      setFocusedId(null);
                    }
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setContextMenuId(null);
                  }}
                  onBlur={(event) => {
                    if (!event.currentTarget.contains(event.relatedTarget)) {
                      setFocusedId(null);
                    }
                  }}
                >
                  <div className="port-forward-cell port-forward-status-cell" role="cell">
                    <span className={`port-forward-status-dot is-${forward.status}`} aria-hidden="true" />
                  </div>
                  <div
                    className="port-forward-cell port-forward-port-cell"
                    role="cell"
                    title={`远端端口 ${forward.remote_host}:${forward.remote_port}`}
                    onDoubleClick={() => beginLabelEdit(forward)}
                  >
                    {editingLabelId === forward.forward_id ? (
                      <form className="port-forward-inline-edit" onSubmit={(event) => void handleChangeLabel(event, forward)}>
                        <input
                          aria-label="端口标签"
                          autoFocus
                          maxLength={120}
                          value={editingLabel}
                          onChange={(event) => setEditingLabel(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Escape") {
                              setEditingLabelId(null);
                              setEditingLabel("");
                            }
                          }}
                          disabled={changingLabelId === forward.forward_id}
                        />
                      </form>
                    ) : (
                      <span className="port-forward-port-label">{portLabel(forward)}</span>
                    )}
                    <button
                      type="button"
                      className="port-forward-inline-action port-forward-label-action"
                      aria-label={`编辑端口标签：${portLabel(forward)}`}
                      title="编辑端口标签"
                      onClick={() => beginLabelEdit(forward)}
                    >
                      <span className="codicon codicon-tag" aria-hidden="true" />
                    </button>
                    {forward.status !== "stopped" ? (
                      <button
                        type="button"
                        className="port-forward-inline-action port-forward-stop-action"
                        aria-label={`停止转发：${portLabel(forward)}`}
                        title="停止转发"
                        onClick={() => void handleStop(forward)}
                        disabled={stoppingId === forward.forward_id}
                      >
                        <span className="codicon codicon-x" aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>
                  <div className="port-forward-cell port-forward-address-cell" role="cell">
                    <div className="port-forward-address-line">
                      {canOpen ? (
                        <a href={forward.local_url!} target="_blank" rel="noreferrer" title="跟随链接打开">
                          {localAddress(forward)}
                        </a>
                      ) : <span>{localAddress(forward)}</span>}
                      <div className="port-forward-actionbar" aria-label="端口操作">
                        <button type="button" className="port-forward-inline-action" aria-label="复制本地地址" title={copiedId === forward.forward_id ? "已复制本地地址" : "复制本地地址"} onClick={() => void handleCopyAddress(forward)} disabled={!forward.local_url}>
                          <span className={`codicon ${copiedId === forward.forward_id ? "codicon-check" : "codicon-clippy"}`} aria-hidden="true" />
                        </button>
                        <button type="button" className="port-forward-inline-action" aria-label="在浏览器中打开" title="在浏览器中打开" onClick={() => handleOpen(forward, "browser")} disabled={!canOpen}>
                          <span className="codicon codicon-globe" aria-hidden="true" />
                        </button>
                        <button type="button" className="port-forward-inline-action" aria-label="在编辑器中预览" title="在编辑器中预览" onClick={() => handleOpen(forward, "preview")} disabled={!canOpen}>
                          <span className="codicon codicon-open-preview" aria-hidden="true" />
                        </button>
                        {forward.status === "error" || forward.status === "stopped" ? (
                          <button
                            type="button"
                            className="port-forward-inline-action"
                            aria-label="重新连接端口"
                            title={reconnectingId === forward.forward_id ? "正在重连" : "重新连接"}
                            onClick={() => void handleReconnect(forward)}
                            disabled={reconnectingId === forward.forward_id}
                          >
                            <span className="codicon codicon-refresh" aria-hidden="true" />
                          </button>
                        ) : null}
                        <details className="port-forward-more">
                          <summary aria-label={`更多端口操作：${portLabel(forward)}`} title="更多端口操作">
                            <span className="codicon codicon-ellipsis" aria-hidden="true" />
                          </summary>
                          <div role="menu">
                            <button type="button" role="menuitem" onClick={() => void handleCopyAddress(forward)} disabled={!forward.local_url}>复制本地地址</button>
                            <button type="button" role="menuitem" onClick={() => handleOpen(forward, "browser")} disabled={!canOpen}>在浏览器中打开</button>
                            <button type="button" role="menuitem" onClick={() => handleOpen(forward, "preview")} disabled={!canOpen}>在编辑器中预览</button>
                            <button type="button" role="menuitem" onClick={() => beginLabelEdit(forward)}>编辑端口标签</button>
                            {forward.local_port ? <button type="button" role="menuitem" onClick={() => { setEditingLocalPortId(forward.forward_id); setEditingLocalPort(String(forward.local_port)); setError(null); }}>更改本地端口</button> : null}
                            {forward.status === "error" || forward.status === "stopped" ? <button type="button" role="menuitem" onClick={() => void handleReconnect(forward)} disabled={reconnectingId === forward.forward_id}>{reconnectingId === forward.forward_id ? "正在重连…" : "重新连接"}</button> : null}
                            {forward.status !== "stopped" ? <button type="button" role="menuitem" onClick={() => void handleStop(forward)} disabled={stoppingId === forward.forward_id}>停止转发</button> : null}
                          </div>
                        </details>
                      </div>
                    </div>
                    {forward.error ? (
                      <div className="port-forward-item-error" role="alert">
                        <span className="port-forward-error-summary" title={forward.error}>{errorSummary(forward.error)}</span>
                        <details className="port-forward-error-details">
                          <summary>查看完整错误</summary>
                          <pre>{forward.error}</pre>
                        </details>
                      </div>
                    ) : null}
                  </div>
                  <div className="port-forward-cell port-forward-process-cell" role="cell" title={processDescription || undefined}>{processDescription}</div>
                  <div className="port-forward-cell port-forward-origin-cell" role="cell">用户转发</div>
                  {editingLocalPortId === forward.forward_id ? (
                    <form className="port-forward-edit-form" onSubmit={(event) => void handleChangeLocalPort(event, forward)}>
                      <label><span>本地端口</span><input type="number" min="1" max="65535" inputMode="numeric" value={editingLocalPort} onChange={(event) => setEditingLocalPort(event.target.value)} disabled={changingLocalPortId === forward.forward_id} autoFocus /></label>
                      <button type="submit" disabled={changingLocalPortId === forward.forward_id || !isValidPortInput(editingLocalPort)}>{changingLocalPortId === forward.forward_id ? "保存中…" : "保存"}</button>
                      <button type="button" onClick={() => { setEditingLocalPortId(null); setEditingLocalPort(""); }} disabled={changingLocalPortId === forward.forward_id}>取消</button>
                    </form>
                  ) : null}
                  {contextMenuId === forward.forward_id ? (
                    <div
                      className="port-forward-context-menu"
                      role="menu"
                      aria-label={`端口上下文菜单：${portLabel(forward)}`}
                      onPointerDown={(event) => event.stopPropagation()}
                    >
                      <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); void handleCopyAddress(forward); }} disabled={!forward.local_url}>复制本地地址</button>
                      <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); handleOpen(forward, "browser"); }} disabled={!canOpen}>在浏览器中打开</button>
                      <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); handleOpen(forward, "preview"); }} disabled={!canOpen}>在编辑器中预览</button>
                      <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); beginLabelEdit(forward); }}>编辑端口标签</button>
                      {forward.local_port ? <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); setEditingLocalPortId(forward.forward_id); setEditingLocalPort(String(forward.local_port)); setError(null); }}>更改本地端口</button> : null}
                      {forward.status === "error" || forward.status === "stopped" ? <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); void handleReconnect(forward); }} disabled={reconnectingId === forward.forward_id}>重新连接</button> : null}
                      {forward.status !== "stopped" ? <button type="button" role="menuitem" onClick={() => { setContextMenuId(null); void handleStop(forward); }} disabled={stoppingId === forward.forward_id}>停止转发</button> : null}
                    </div>
                  ) : null}
                </article>
              );
            })}
            {isTableMenuOpen ? (
              <div
                className="port-forward-context-menu port-forward-table-context-menu"
                role="menu"
                aria-label="端口列表操作"
                onPointerDown={(event) => event.stopPropagation()}
              >
                <button type="button" role="menuitem" onClick={() => { setIsTableMenuOpen(false); setIsFilterOpen(true); }}>筛选端口</button>
                <button type="button" role="menuitem" aria-expanded={isCreateOpen} aria-controls="workspace-port-forward-form" onClick={() => { setIsTableMenuOpen(false); setIsCreateOpen((open) => !open); }} disabled={submitting}>{isCreateOpen ? "取消新增端口" : "新增端口"}</button>
                <button type="button" role="menuitem" onClick={() => { setIsTableMenuOpen(false); void refresh(); }} disabled={loading || submitting}>刷新端口</button>
              </div>
            ) : null}
            </div>
          </>
        ) : null}
      </div>

      {isCreateOpen ? (
        <div className="port-forward-create-region">
          <form id="workspace-port-forward-form" className="port-forward-form" onSubmit={(event) => void handleCreate(event)}>
            <label className="port-forward-field port-forward-field-primary">
              <span>远端端口</span>
              <input type="number" min="1" max="65535" required inputMode="numeric" placeholder="例如 5173" value={remotePort} onChange={(event) => setRemotePort(event.target.value)} disabled={submitting} />
            </label>
            <label className="port-forward-field">
              <span>本地端口（可选）</span>
              <input type="number" min="1" max="65535" inputMode="numeric" placeholder="自动分配" value={localPort} onChange={(event) => setLocalPort(event.target.value)} disabled={submitting} />
            </label>
            <label className="port-forward-field">
              <span>协议</span>
              <select value={protocol} onChange={(event) => setProtocol(event.target.value as GatewayPortForwardProtocol)} disabled={submitting}>
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="tcp">TCP</option>
              </select>
            </label>
            <label className="port-forward-field">
              <span>名称（可选）</span>
              <input type="text" maxLength={120} placeholder="例如 Vite 开发服务器" value={label} onChange={(event) => setLabel(event.target.value)} disabled={submitting} />
            </label>
            <button className="port-forward-create" type="submit" disabled={createDisabled}>{submitting ? "正在创建…" : "创建转发"}</button>
            <button className="port-forward-create-cancel" type="button" onClick={() => setIsCreateOpen(false)} disabled={submitting}>取消</button>
          </form>
        </div>
      ) : null}
    </section>
  );
}
