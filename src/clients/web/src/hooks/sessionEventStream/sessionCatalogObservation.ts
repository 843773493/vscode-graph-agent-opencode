import {
  streamSessionCatalogSnapshots,
  type SessionCatalogSnapshot,
} from "../../api/session/sessionCatalogOperations";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import {
  SESSION_STREAM_MAX_RECONNECT_ATTEMPTS,
  sessionStreamReconnectDelay,
} from "./sessionEventStreamPolicy";
import { waitForReconnect } from "./waitForReconnect";

/** 串行消费最新通知；同步失败也重连，首帧快照让原游标重新对账。 */
export async function observeSessionCatalogChanges(
  port: number,
  target: GatewayWorkspaceRequestTarget,
  options: {
    signal: AbortSignal;
    onSnapshot: (snapshot: SessionCatalogSnapshot) => Promise<void>;
    onError: (error: unknown) => void;
  },
): Promise<void> {
  let attempt = 0;
  while (!options.signal.aborted) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal.addEventListener("abort", abort, { once: true });
    let pending: SessionCatalogSnapshot | null = null;
    let syncing: Promise<void> | null = null;
    let failure: unknown;
    try {
      await streamSessionCatalogSnapshots(port, target, {
        signal: controller.signal,
        onSnapshot(snapshot) {
          pending = snapshot;
          if (syncing) return;
          syncing = Promise.resolve().then(async () => {
            try {
              while (pending && !controller.signal.aborted) {
                const current = pending;
                pending = null;
                await options.onSnapshot(current);
                attempt = 0;
              }
            } catch (error) {
              failure = error;
              controller.abort();
            } finally {
              syncing = null;
            }
          });
        },
      });
      if (syncing) await syncing;
      if (options.signal.aborted) return;
      throw failure ?? new Error("会话目录推送连接已关闭");
    } catch (error) {
      if (options.signal.aborted) return;
      const cause = failure ?? error;
      options.onError(cause);
      if (attempt >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS) {
        throw new Error(`会话目录推送连续重连失败，已停止自动重连；请刷新页面后重试：${String(cause)}`);
      }
      await waitForReconnect(options.signal, sessionStreamReconnectDelay(attempt++));
    } finally {
      options.signal.removeEventListener("abort", abort);
      controller.abort();
      if (syncing) await syncing;
    }
  }
}
