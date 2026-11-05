import { useEffect, useRef, useState } from "react";
import { locateGatewayDiagnosticLog } from "../../../gatewayApi";
import type { GatewayWorkspace } from "../../../types/backend";
import type { GatewayDiagnosticLogLocateRequest, GatewayDiagnosticLogLocationDTO } from "../../../types/protocol_generated/boxteam/gateway/v2/public";
import { errorMessage } from "../../../utils/errorMessage";

export default function GatewayDiagnosticLogLocation({ apiPort, request, workspaces, onClose }: {
  apiPort: number; request: GatewayDiagnosticLogLocateRequest; workspaces: GatewayWorkspace[]; onClose: () => void;
}) {
  const [snapshot, setSnapshot] = useState<{ request: GatewayDiagnosticLogLocateRequest; data?: GatewayDiagnosticLogLocationDTO; error?: string }>();
  const mark = useRef<HTMLElement>(null);
  const current = snapshot?.request === request ? snapshot : undefined;
  useEffect(() => {
    const controller = new AbortController();
    void locateGatewayDiagnosticLog(apiPort, request, { signal: controller.signal }).then(
      data => { if (!controller.signal.aborted) setSnapshot({ request, data }); },
      cause => { if (!controller.signal.aborted) setSnapshot({ request, error: errorMessage(cause) }); },
    );
    return () => controller.abort();
  }, [apiPort, request]);
  useEffect(() => { mark.current?.scrollIntoView({ block: "center", inline: "nearest" }); }, [current?.data]);
  const data = current?.data, reference = data?.reference;
  const text = data ? Array.from(data.text) : [];
  const linkedWorkspaces = data?.source?.workspace_ids.map(id => workspaces.find(workspace =>
    workspace.gateway_id === reference?.gateway_id && workspace.workspace_id === id)?.name ?? id);
  return <section className="gateway-diagnostics-viewer" aria-label="运行日志原行定位">
    <header><strong>{data?.source?.label ?? "运行日志原行"}</strong><button type="button" onClick={onClose}>关闭定位</button></header>
    <p>{request.reference?.gateway_id} · {request.scope?.source === "gateway" ? "Gateway 自有来源" : `受管进程 · ${linkedWorkspaces?.join("、") ?? "读取关联工作区…"}${(linkedWorkspaces?.length ?? 0) > 1 ? "（共享来源）" : ""}`}</p>
    {current?.error ? <p role="alert">日志定位失败：{current.error}</p> : !data ? <p role="status">正在核验原日志行…</p> : reference ? <>
      <p>原行字节区间 {reference.line_start}–{reference.line_end} · 正文码点 {reference.start_codepoint}–{reference.end_codepoint}</p>
      <pre data-testid="gateway-diagnostic-log-location">{text.slice(0, reference.start_codepoint).join("")}<mark ref={mark}
        data-start-codepoint={reference.start_codepoint} data-end-codepoint={reference.end_codepoint}>{text.slice(reference.start_codepoint, reference.end_codepoint).join("")}</mark>{text.slice(reference.end_codepoint).join("")}</pre>
    </> : <p role="alert">日志定位响应缺少原记录引用</p>}
  </section>;
}
