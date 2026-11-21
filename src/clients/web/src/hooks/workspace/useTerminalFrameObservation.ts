import { useEffect, type RefObject } from "react";
import { observeTerminalResources } from "../../api/workspaceResources";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

/** iframe 只登记可见订阅；网络观察由宿主统一实时入口按工作区共享逻辑订阅。 */
export function useTerminalFrameObservation(frame: RefObject<HTMLIFrameElement>, source: string | null): void {
  useEffect(() => {
    if (!source) return;
    const url = new URL(source, window.location.origin);
    const terminalId = url.searchParams.get("terminalId");
    const workspaceId = url.searchParams.get("workspaceId");
    const gatewayId = url.searchParams.get("target_gateway_id");
    if (!terminalId || !workspaceId || !gatewayId) return;
    const workspaceKey = gatewayWorkspaceKey({ gatewayId, workspaceId });
    let controller: AbortController | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let subscriptionId: string | undefined;
    const stop = () => { controller?.abort(); controller = null; clearTimeout(retry); };
    const start = (id: string, target: Window) => {
      const observing = new AbortController();
      controller = observing;
      const post = (error?: string) => target.postMessage({
        type: "boxteam.terminal.resources", terminalId, subscriptionId: id, error,
      }, window.location.origin);
      void observeTerminalResources(0, workspaceKey, ids => {
        if (!observing.signal.aborted && (ids === null || ids.includes(terminalId))) post();
      }, observing.signal).then(() => {
        if (!observing.signal.aborted) throw new Error("终端资源观察流已结束");
      }).catch(cause => {
        if (observing.signal.aborted) return;
        post(errorMessage(cause));
        retry = setTimeout(() => start(id, target), 5000);
      });
    };
    const receive = (event: MessageEvent<unknown>) => {
      if (event.origin !== window.location.origin || event.source !== frame.current?.contentWindow
        || !event.data || typeof event.data !== "object") return;
      const message = event.data as { type?: string; terminalId?: string; subscriptionId?: string };
      if (message.terminalId !== terminalId || !message.subscriptionId) return;
      if (message.type === "boxteam.terminal.observe") {
        if (subscriptionId === message.subscriptionId && controller) return;
        stop(); subscriptionId = message.subscriptionId;
        start(subscriptionId, frame.current!.contentWindow!);
      } else if (message.type === "boxteam.terminal.unobserve" && message.subscriptionId === subscriptionId) {
        stop(); subscriptionId = undefined;
      }
    };
    window.addEventListener("message", receive);
    return () => { window.removeEventListener("message", receive); stop(); };
  }, [frame, source]);
}
