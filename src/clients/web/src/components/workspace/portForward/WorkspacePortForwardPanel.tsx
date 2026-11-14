import { t } from '../../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  changeWorkspacePortForwardLabel,
  changeWorkspacePortForwardLocalPort,
  createWorkspacePortForward,
  deleteWorkspacePortForward,
  listWorkspacePortForwards,
  reconnectWorkspacePortForward,
} from "../../../gatewayApi";
import type {
  ChangeGatewayPortForwardLabelRequest,
  ChangeGatewayPortForwardLocalPortRequest,
  CreateGatewayPortForwardRequest,
  GatewayPortForward,
  GatewayPortForwardList,
  GatewayPortForwardProtocol,
  GatewayWorkspace,
} from "../../../types/backend";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import { errorMessage } from "../../../utils/errorMessage";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { DECIMAL_PORT_PATTERN, isValidPortLiteral, parsePortLiteral } from "../portLiteral";

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
  starting: t('ports.status.starting'),
  active: t('ports.status.active'),
  error: t('ports.status.error'),
  stopped: t('ports.status.stopped'),
};

// 端口只接受十进制整数字面量：`Number()` 会把 "1e3"、"12.0"、" 80 "、"0x50"
// 静默收敛成数字，导致非法输入被当成合法端口提交。解析与合法性判定共用同一
// 份实现，避免「按钮判定」与「提交解析」两套口径漂移。
const PORT_INPUT_HINT = t('ports.portHint');

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
  const workspaceId = nullableGatewayWorkspaceKey(workspace);
  const isRemoteWorkspace = workspace?.connection_kind === "remote_gateway";
  const currentWorkspaceIdRef = useRef(workspaceId);
  currentWorkspaceIdRef.current = workspaceId;

  const refresh = useCallback(async () => {
    if (workspaceId === null || !isRemoteWorkspace || !active) return;
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
        setError(t('ports.refreshFailed', errorMessage(loadError)));
      }
    } finally {
      if (requestRevisionRef.current === revision) setLoading(false);
    }
  }, [active, api, apiPort, isRemoteWorkspace, workspaceId]);

  useEffect(() => {
    setForwards(null);
    setLoading(false);
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
  }, [apiPort, workspaceId]);

  useEffect(() => {
    requestRevisionRef.current += 1;
    void refresh();
  }, [
    refresh,
    workspace?.connection_error,
    workspace?.remote_gateways,
    workspace?.status,
  ]);

  const recoverAfterFailure = async (workspaceId: string, message: string) => {
    setError(message);
    try {
      const recovered = await api.list(apiPort, workspaceId);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(recovered);
    } catch (recoverError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        setError(t('ports.recoveryFailed', message, errorMessage(recoverError)));
      }
    }
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!workspace || workspace.connection_kind !== "remote_gateway") return;
    let payload: CreateGatewayPortForwardRequest;
    try {
      payload = {
        remote_port: parsePortLiteral(remotePort, t('ports.remotePort')),
        local_port: localPort ? parsePortLiteral(localPort, t('ports.localPort')) : null,
        protocol,
        label: label.trim() || null,
      };
    } catch (validationError) {
      setError(errorMessage(validationError));
      return;
    }
    const workspaceId = gatewayWorkspaceKey(workspace);
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
        await recoverAfterFailure(workspaceId, t('ports.createFailed', errorMessage(createError)));
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
          title: t('ports.stopTitle'),
          message: t('ports.stopMessage', forward.label || t('ports.remotePortNumber', forward.remote_port), forward.local_host, forward.local_port),
          confirmText: t('ports.stop'),
          danger: true,
        });
    if (!accepted) return;
    const workspaceId = gatewayWorkspaceKey(workspace);
    setStoppingId(forward.forward_id);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.remove(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(result);
    } catch (stopError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, t('ports.stopFailed', errorMessage(stopError)));
      }
    } finally {
      if (currentWorkspaceIdRef.current === workspaceId) setStoppingId(null);
    }
  };

  const handleCopyAddress = async (forward: GatewayPortForward) => {
    if (!forward.local_url) return;
    // 复制统一走 utils/clipboard 的唯一实现：非安全上下文下 navigator.clipboard 真实
    // 缺失，必须回退兼容复制，不能把「没有 Clipboard API」直接判成复制失败。
    try {
      await copyTextToClipboard(forward.local_url);
      setCopiedId(forward.forward_id);
      setCopyRetryId(null);
      window.setTimeout(() => {
        setCopiedId((current) => current === forward.forward_id ? null : current);
      }, 1400);
    } catch (copyError) {
      setCopyRetryId(forward.forward_id);
      setError(t('ports.copyFailed', errorMessage(copyError)));
    }
  };

  const handleOpen = (forward: GatewayPortForward, mode: "browser" | "preview") => {
    if (!forward.local_url) return;
    const target = mode === "preview" ? "boxteam-port-preview" : "_blank";
    const opened = window.open(forward.local_url, target, "noopener,noreferrer");
    if (!opened) setError(t('ports.popupBlocked'));
  };

  const handleReconnect = async (forward: GatewayPortForward) => {
    if (!workspace) return;
    const workspaceId = gatewayWorkspaceKey(workspace);
    setReconnectingId(forward.forward_id);
    setError(null);
    setCopyRetryId(null);
    try {
      const result = await api.reconnect(apiPort, workspaceId, forward.forward_id);
      if (currentWorkspaceIdRef.current === workspaceId) setForwards(result);
    } catch (reconnectError) {
      if (currentWorkspaceIdRef.current === workspaceId) {
        await recoverAfterFailure(workspaceId, t('ports.reconnectFailed', errorMessage(reconnectError)));
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
      payload = { local_port: parsePortLiteral(editingLocalPort, t('ports.localPort')) };
    } catch (validationError) {
      setError(errorMessage(validationError));
      return;
    }
    const workspaceId = gatewayWorkspaceKey(workspace);
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
        await recoverAfterFailure(workspaceId, t('ports.changeLocalPortFailed', errorMessage(changeError)));
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
    const workspaceId = gatewayWorkspaceKey(workspace);
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
        await recoverAfterFailure(workspaceId, t('ports.changeLabelFailed', errorMessage(changeError)));
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
        <p>{t('ports.noWorkspace')}</p>
      </section>
    );
  }

  if (workspace.connection_kind !== "remote_gateway") {
    return (
      <section className="port-forward-panel port-forward-panel-empty" aria-label={t('ports.workspacePorts')}>
        <p className="port-forward-local-empty">{t('ports.localWorkspace')}</p>
      </section>
    );
  }

  const normalizedFilter = filterText.trim().toLocaleLowerCase();
  // 端口是数字而不是字符串：按 "80" 筛选不得命中 8013 / 41080。只有能构成端口的纯十进制
  // 输入才走端口精确匹配（远端或本地端口相等），其余输入退回对标签、协议、转发地址与状态的
  // 子串过滤，保留按名称/协议的模糊查找能力。
  const portFilter = DECIMAL_PORT_PATTERN.test(normalizedFilter) ? Number(normalizedFilter) : null;
  const filteredForwards = forwards?.items.filter((forward) => portFilter === null
    ? [forward.label, forward.protocol, forward.local_url, forward.status]
        .some((value) => String(value ?? "").toLocaleLowerCase().includes(normalizedFilter))
    : forward.remote_port === portFilter || forward.local_port === portFilter) ?? [];
  const createDisabled = submitting || !isValidPortLiteral(remotePort) || (localPort !== "" && !isValidPortLiteral(localPort));
  // 输入不合法时按钮会被禁用；必须同时给出原因，避免「点了没反应」的静默失败。
  const remotePortInvalid = remotePort !== "" && !isValidPortLiteral(remotePort);
  const localPortInvalid = localPort !== "" && !isValidPortLiteral(localPort);

  // 行内「更多操作」菜单与右键上下文菜单逐字同族：两者此前各写一份完全相同的
  // 条目与禁用条件，新增一个端口动作就要改两处、极易只改一处。收敛成唯一一份，
  // 由调用方决定点击时是否需要先关闭右键菜单。
  const renderPortActionItems = (
    forward: GatewayPortForward,
    canOpen: boolean,
    beforeAction: () => void,
  ) => (
    <>
      <button type="button" role="menuitem" onClick={() => { beforeAction(); void handleCopyAddress(forward); }} disabled={!forward.local_url}>{t('ports.copyAddress')}</button>
      <button type="button" role="menuitem" onClick={() => { beforeAction(); handleOpen(forward, "browser"); }} disabled={!canOpen}>{t('ports.openBrowser')}</button>
      <button type="button" role="menuitem" onClick={() => { beforeAction(); handleOpen(forward, "preview"); }} disabled={!canOpen}>{t('ports.openPreview')}</button>
      <button type="button" role="menuitem" onClick={() => { beforeAction(); beginLabelEdit(forward); }}>{t('ports.editLabel')}</button>
      {forward.local_port ? <button type="button" role="menuitem" onClick={() => { beforeAction(); setEditingLocalPortId(forward.forward_id); setEditingLocalPort(String(forward.local_port)); setError(null); }}>{t('ports.changeLocalPort')}</button> : null}
      {forward.status === "error" || forward.status === "stopped" ? <button type="button" role="menuitem" onClick={() => { beforeAction(); void handleReconnect(forward); }} disabled={reconnectingId === forward.forward_id}>{reconnectingId === forward.forward_id ? t('ports.reconnecting') : t('ports.reconnect')}</button> : null}
      {forward.status !== "stopped" ? <button type="button" role="menuitem" onClick={() => { beforeAction(); void handleStop(forward); }} disabled={stoppingId === forward.forward_id}>{t('ports.stop')}</button> : null}
    </>
  );

  return (
    <section className="port-forward-panel" aria-label={t('ports.panel')}>
      <div className="port-forward-feedback-slot">
        {error ? (
          <div className="port-forward-error" role="alert" title={error}>
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
                {t('ports.retryCopy')}
              </button>
            ) : (
              <button type="button" onClick={() => void refresh()} disabled={loading}>{t('ports.retryRefresh')}</button>
            )}
          </div>
        ) : null}
      </div>

      <div className="port-forward-list" aria-live="polite">
        {loading && !forwards ? <p className="port-forward-empty">{t('ports.loading')}</p> : null}
        {!loading && forwards?.items.length === 0 ? (
          <p className="port-forward-empty">
            {t('ports.empty')}
            <button
              type="button"
              className="port-forward-empty-action"
              aria-expanded={isCreateOpen}
              aria-controls="workspace-port-forward-form"
              onClick={() => setIsCreateOpen((open) => !open)}
              disabled={submitting}
            >
              {isCreateOpen ? t('ports.cancel') : t('ports.add')}
            </button>
          </p>
        ) : null}
        {!loading && forwards && forwards.items.length > 0 && filteredForwards.length === 0 ? <p className="port-forward-empty">{t('ports.noResults')}</p> : null}
        {isFilterOpen ? (
          <label className="port-forward-inline-filter" aria-label={t('ports.filter')}>
            <span className="codicon codicon-search" aria-hidden="true" />
            <input
              ref={filterInputRef}
              type="search"
              placeholder={t('ports.filterHint')}
              value={filterText}
              onChange={(event) => setFilterText(event.target.value)}
            />
            <button type="button" aria-label={t('ports.closeFilter')} onClick={() => { setIsFilterOpen(false); setFilterText(""); }}>
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </label>
        ) : null}
        {filteredForwards.length > 0 ? (
          <>
            <p className="port-forward-narrow-hint">{t('ports.horizontalScrollHint')}</p>
            <div
              className="port-forward-table ports-view"
              role="table"
              aria-label={t('ports.table')}
              onContextMenu={(event) => {
                event.preventDefault();
                setContextMenuId(null);
                setIsTableMenuOpen(true);
              }}
            >
            <div className="port-forward-table-header" role="row">
              <span role="columnheader" aria-label={t('ports.portStatus')} />
              <span role="columnheader">{t('ports.port')}</span>
              <span role="columnheader">{t('ports.address')}</span>
              <span role="columnheader">{t('ports.process')}</span>
              <span role="columnheader">{t('ports.source')}</span>
            </div>
            {filteredForwards.map((forward) => {
              const canOpen = forward.status === "active" && forward.protocol !== "tcp" && Boolean(forward.local_url);
              const isFocused = focusedId === forward.forward_id;
              const processDescription = forward.status === "error" ? t('ports.processUnavailable') : "";
              return (
                <article
                  className={`port-forward-row is-${forward.status}${isFocused ? " is-focused" : ""}`}
                  key={forward.forward_id}
                  role="row"
                  tabIndex={0}
                  aria-label={t('ports.row', portLabel(forward), STATUS_LABELS[forward.status])}
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
                    title={t('ports.remoteAddress', forward.remote_host, forward.remote_port)}
                    onDoubleClick={() => beginLabelEdit(forward)}
                  >
                    {editingLabelId === forward.forward_id ? (
                      <form className="port-forward-inline-edit" onSubmit={(event) => void handleChangeLabel(event, forward)}>
                        <input
                          aria-label={t('ports.label')}
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
                      aria-label={t('ports.editLabelFor', portLabel(forward))}
                      title={t('ports.editLabel')}
                      onClick={() => beginLabelEdit(forward)}
                    >
                      <span className="codicon codicon-tag" aria-hidden="true" />
                    </button>
                    {forward.status !== "stopped" ? (
                      <button
                        type="button"
                        className="port-forward-inline-action port-forward-stop-action"
                        aria-label={t('ports.stopFor', portLabel(forward))}
                        title={t('ports.stop')}
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
                        <a href={forward.local_url!} target="_blank" rel="noreferrer" title={t('ports.followLink')}>
                          {localAddress(forward)}
                        </a>
                      ) : <span>{localAddress(forward)}</span>}
                      <div className="port-forward-actionbar" aria-label={t('ports.actions')}>
                        <button type="button" className="port-forward-inline-action" aria-label={t('ports.copyAddress')} title={copiedId === forward.forward_id ? t('ports.addressCopied') : t('ports.copyAddress')} onClick={() => void handleCopyAddress(forward)} disabled={!forward.local_url}>
                          <span className={`codicon ${copiedId === forward.forward_id ? "codicon-check" : "codicon-clippy"}`} aria-hidden="true" />
                        </button>
                        <button type="button" className="port-forward-inline-action" aria-label={t('ports.openBrowser')} title={t('ports.openBrowser')} onClick={() => handleOpen(forward, "browser")} disabled={!canOpen}>
                          <span className="codicon codicon-globe" aria-hidden="true" />
                        </button>
                        <button type="button" className="port-forward-inline-action" aria-label={t('ports.openPreview')} title={t('ports.openPreview')} onClick={() => handleOpen(forward, "preview")} disabled={!canOpen}>
                          <span className="codicon codicon-open-preview" aria-hidden="true" />
                        </button>
                        {forward.status === "error" || forward.status === "stopped" ? (
                          <button
                            type="button"
                            className="port-forward-inline-action"
                            aria-label={t('ports.reconnectPort')}
                            title={reconnectingId === forward.forward_id ? t('ports.reconnectingShort') : t('ports.reconnect')}
                            onClick={() => void handleReconnect(forward)}
                            disabled={reconnectingId === forward.forward_id}
                          >
                            <span className="codicon codicon-refresh" aria-hidden="true" />
                          </button>
                        ) : null}
                        <details className="port-forward-more">
                          <summary aria-label={t('ports.moreActionsFor', portLabel(forward))} title={t('ports.moreActions')}>
                            <span className="codicon codicon-ellipsis" aria-hidden="true" />
                          </summary>
                          <div role="menu">
                            {renderPortActionItems(forward, canOpen, () => undefined)}
                          </div>
                        </details>
                      </div>
                    </div>
                    {forward.error ? (
                      <div className="port-forward-item-error" role="alert">
                        <span className="port-forward-error-summary" title={forward.error}>{errorSummary(forward.error)}</span>
                        <details className="port-forward-error-details">
                          <summary>{t('ports.fullError')}</summary>
                          <pre>{forward.error}</pre>
                        </details>
                      </div>
                    ) : null}
                  </div>
                  <div className="port-forward-cell port-forward-process-cell" role="cell" title={processDescription || undefined}>{processDescription}</div>
                  <div className="port-forward-cell port-forward-origin-cell" role="cell">{t('ports.userForwarded')}</div>
                  {editingLocalPortId === forward.forward_id ? (
                    <form className="port-forward-edit-form" onSubmit={(event) => void handleChangeLocalPort(event, forward)}>
                      <label><span>{t('ports.localPort')}</span><input type="number" min="1" max="65535" inputMode="numeric" value={editingLocalPort} onChange={(event) => setEditingLocalPort(event.target.value)} disabled={changingLocalPortId === forward.forward_id} autoFocus /></label>
                      <button type="submit" disabled={changingLocalPortId === forward.forward_id || !isValidPortLiteral(editingLocalPort)}>{changingLocalPortId === forward.forward_id ? t('ports.saving') : t('ports.save')}</button>
                      <button type="button" onClick={() => { setEditingLocalPortId(null); setEditingLocalPort(""); }} disabled={changingLocalPortId === forward.forward_id}>{t('ports.cancel')}</button>
                      {!isValidPortLiteral(editingLocalPort) ? (
                        <p className="port-forward-port-hint" role="alert">{PORT_INPUT_HINT}</p>
                      ) : null}
                    </form>
                  ) : null}
                  {contextMenuId === forward.forward_id ? (
                    <div
                      className="port-forward-context-menu"
                      role="menu"
                      aria-label={t('ports.contextMenu', portLabel(forward))}
                      onPointerDown={(event) => event.stopPropagation()}
                    >
                      {renderPortActionItems(forward, canOpen, () => setContextMenuId(null))}
                    </div>
                  ) : null}
                </article>
              );
            })}
            {isTableMenuOpen ? (
              <div
                className="port-forward-context-menu port-forward-table-context-menu"
                role="menu"
                aria-label={t('ports.listActions')}
                onPointerDown={(event) => event.stopPropagation()}
              >
                <button type="button" role="menuitem" onClick={() => { setIsTableMenuOpen(false); setIsFilterOpen(true); }}>{t('ports.filter')}</button>
                <button type="button" role="menuitem" aria-expanded={isCreateOpen} aria-controls="workspace-port-forward-form" onClick={() => { setIsTableMenuOpen(false); setIsCreateOpen((open) => !open); }} disabled={submitting}>{isCreateOpen ? t('ports.cancelAdd') : t('ports.add')}</button>
                <button type="button" role="menuitem" onClick={() => { setIsTableMenuOpen(false); void refresh(); }} disabled={loading || submitting}>{t('ports.refresh')}</button>
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
              <span>{t('ports.remotePort')}</span>
              <input type="number" min="1" max="65535" required inputMode="numeric" placeholder={t('ports.remotePortExample')} value={remotePort} onChange={(event) => setRemotePort(event.target.value)} disabled={submitting} aria-invalid={remotePortInvalid} />
            </label>
            <label className="port-forward-field">
              <span>{t('ports.optionalLocalPort')}</span>
              <input type="number" min="1" max="65535" inputMode="numeric" placeholder={t('ports.automaticPort')} value={localPort} onChange={(event) => setLocalPort(event.target.value)} disabled={submitting} aria-invalid={localPortInvalid} />
            </label>
            <label className="port-forward-field">
              <span>{t('ports.protocol')}</span>
              <select value={protocol} onChange={(event) => setProtocol(event.target.value as GatewayPortForwardProtocol)} disabled={submitting}>
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="tcp">TCP</option>
              </select>
            </label>
            <label className="port-forward-field">
              <span>{t('ports.optionalName')}</span>
              <input type="text" maxLength={120} placeholder={t('ports.nameExample')} value={label} onChange={(event) => setLabel(event.target.value)} disabled={submitting} />
            </label>
            <button className="port-forward-create" type="submit" disabled={createDisabled}>{submitting ? t('ports.creating') : t('ports.create')}</button>
            <button className="port-forward-create-cancel" type="button" onClick={() => setIsCreateOpen(false)} disabled={submitting}>{t('ports.cancel')}</button>
          </form>
          {remotePortInvalid || localPortInvalid ? (
            <p className="port-forward-port-hint" role="alert">
              {PORT_INPUT_HINT}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
