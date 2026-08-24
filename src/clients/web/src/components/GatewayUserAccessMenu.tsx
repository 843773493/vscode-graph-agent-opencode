import { useEffect, useRef, useState } from "react";
import {
  acquireGatewayGuest,
  createGatewayUser,
  deleteGatewayUser,
  heartbeatGatewayUser,
  listGatewayUsers,
  selectGatewayUser,
  takeoverGatewayUser,
} from "../gatewayApi";
import { HttpRequestError } from "../api";
import { useAppState } from "../hooks";
import AnchoredOverlay from "./AnchoredOverlay";

function errorMessage(error: unknown): string {
  if (error instanceof HttpRequestError && error.detail && typeof error.detail === "object") {
    const detail = error.detail as { code?: unknown; client_label?: unknown };
    if (detail.code === "user_lease_occupied") {
      return `用户正在被占用${typeof detail.client_label === "string" ? `（${detail.client_label}）` : ""}`;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

export default function GatewayUserAccessMenu() {
  const { state, refreshGatewayState, setStatus } = useAppState();
  const [open, setOpen] = useState(false);
  const [users, setUsers] = useState<Awaited<ReturnType<typeof listGatewayUsers>>["items"]>([]);
  const [loading, setLoading] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const apiPort = state.apiPort ?? 8014;
  const current = state.gatewayUserAccess;

  const refreshUsers = async () => {
    setLoading(true);
    setError(null);
    try {
      setUsers((await listGatewayUsers(apiPort)).items);
    } catch (cause: unknown) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!current) return;
    const timer = window.setInterval(() => {
      void heartbeatGatewayUser(apiPort).catch((cause: unknown) => {
        if (cause instanceof HttpRequestError && cause.status === 409) {
          void refreshGatewayState()
            .then(() => {
              setStatus("当前用户已被另一台电脑接管，已切换到游客视图");
            })
            .catch((refreshError: unknown) => {
              setError(`当前访问已失效，且游客视图切换失败：${errorMessage(refreshError)}`);
            });
          return;
        }
        setError(`当前访问已失效：${errorMessage(cause)}`);
      });
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [apiPort, current, refreshGatewayState, setStatus]);

  const runAccessChange = async (operation: () => Promise<unknown>, success: string) => {
    setLoading(true);
    setError(null);
    try {
      await operation();
      await refreshGatewayState();
      setStatus(success);
      setOpen(false);
      await refreshUsers();
    } catch (cause: unknown) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async () => {
    const name = displayName.trim();
    if (!name) {
      setError("请输入用户名称");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const user = await createGatewayUser(apiPort, { display_name: name });
      setDisplayName("");
      await runAccessChange(
        () => selectGatewayUser(apiPort, user.user_id, "Web 浏览器"),
        `已切换到用户 ${user.display_name}`,
      );
    } catch (cause: unknown) {
      setError(errorMessage(cause));
      setLoading(false);
    }
  };

  return (
    <div className="gateway-user-access" ref={anchorRef}>
      <button
        type="button"
        className={`toolbar-icon-button gateway-user-access-button${open ? " active" : ""}`}
        aria-label="用户视图"
        title={current?.kind === "guest" ? "游客视图" : `用户视图 ${current?.user_id ?? ""}`}
        aria-expanded={open}
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next) void refreshUsers();
        }}
      >
        <span className="codicon codicon-account" aria-hidden="true" />
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={anchorRef}
        placement="bottom-end"
        onClose={() => setOpen(false)}
      >
        <div className="gateway-user-access-menu" role="dialog" aria-label="用户视图">
          <div className="gateway-user-access-heading">
            <strong>{current?.kind === "guest" ? "游客视图" : `用户 ${current?.user_id ?? ""}`}</strong>
            <small>同一用户同时只允许一个访问设备</small>
          </div>
          <div className="gateway-user-create-row">
            <input
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="新用户名称"
              aria-label="新用户名称"
              maxLength={120}
            />
            <button type="button" onClick={() => void handleCreate()} disabled={loading}>创建</button>
          </div>
          <div className="gateway-user-list">
            {users.map((user) => (
              <div className="gateway-user-row" key={user.user_id}>
                <div>
                  <strong>{user.display_name}</strong>
                  <small>{user.user_id}</small>
                  {user.lease.occupied ? <em>占用中{user.lease.client_label ? ` · ${user.lease.client_label}` : ""}</em> : null}
                </div>
                <div className="gateway-user-row-actions">
                  <button
                    type="button"
                    onClick={() => void runAccessChange(
                      () => selectGatewayUser(apiPort, user.user_id, "Web 浏览器"),
                      `已切换到用户 ${user.display_name}`,
                    )}
                    disabled={loading || user.lease.occupied}
                  >选择</button>
                  {user.lease.occupied ? <button
                    type="button"
                    onClick={() => void runAccessChange(
                      () => takeoverGatewayUser(apiPort, user.user_id, "Web 浏览器"),
                      `已接管用户 ${user.display_name}`,
                    )}
                    disabled={loading}
                  >接管</button> : null}
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void runAccessChange(
                      () => deleteGatewayUser(apiPort, user.user_id),
                      `已删除用户 ${user.display_name}`,
                    )}
                    disabled={loading || user.lease.occupied}
                  >删除</button>
                </div>
              </div>
            ))}
          </div>
          <button
            type="button"
            className="gateway-user-guest-button"
            onClick={() => void runAccessChange(
              () => acquireGatewayGuest(apiPort),
              "已切换到游客视图",
            )}
            disabled={loading || current?.kind === "guest"}
          >使用游客视图</button>
          {error ? <div className="gateway-user-access-error" role="alert">{error}</div> : null}
        </div>
      </AnchoredOverlay>
    </div>
  );
}
