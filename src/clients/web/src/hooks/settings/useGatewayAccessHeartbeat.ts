import { useEffect } from "react";
import { heartbeatGatewayUserWithRetry } from "../../api/gateway/userAccess";
import { errorMessage } from "../../utils/errorMessage";

/** 访问凭据生命周期与本地视图完全分离；不再由视图菜单暗中维持租约。 */
export function useGatewayAccessHeartbeat(port: number | null, ready: boolean, refresh: () => Promise<unknown>, setStatus: (message: string) => void): void {
  useEffect(() => {
    if (port === null || !ready) return;
    const timer = window.setInterval(() => {
      void heartbeatGatewayUserWithRetry(port).catch(async error => {
        setStatus(`Gateway 访问凭据刷新失败：${errorMessage(error)}`);
        await refresh();
      }).catch(error => setStatus(`Gateway 访问恢复失败：${errorMessage(error)}`));
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [port, ready, refresh, setStatus]);
}
