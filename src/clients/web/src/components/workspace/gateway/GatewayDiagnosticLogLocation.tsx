import { useEffect, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
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
  return <section className="gateway-diagnostics-viewer" aria-label={t("gateway.diagnostics.locationPanel")}>
    <header><strong>{data?.source?.label ?? t("gateway.diagnostics.originalLogLine")}</strong><button type="button" onClick={onClose}>{t("gateway.diagnostics.closeLocation")}</button></header>
    <p>{request.reference?.gateway_id} · {request.scope?.source === "gateway" ? t("gateway.diagnostics.gatewaySource") : t("gateway.diagnostics.managedProcessSource", linkedWorkspaces?.join(t("gateway.diagnostics.workspaceListSeparator")) ?? t("gateway.diagnostics.loadingRelatedWorkspaces"), (linkedWorkspaces?.length ?? 0) > 1 ? t("gateway.diagnostics.sharedSourceSuffix") : "")}</p>
    {current?.error ? <p role="alert">{t("gateway.diagnostics.locationFailed", current.error)}</p> : !data ? <p role="status">{t("gateway.diagnostics.verifyingOriginalLine")}</p> : reference ? <>
      <p>{t("gateway.diagnostics.originalLineRanges", reference.line_start, reference.line_end, reference.start_codepoint, reference.end_codepoint)}</p>
      <pre data-testid="gateway-diagnostic-log-location">{text.slice(0, reference.start_codepoint).join("")}<mark ref={mark}
        data-start-codepoint={reference.start_codepoint} data-end-codepoint={reference.end_codepoint}>{text.slice(reference.start_codepoint, reference.end_codepoint).join("")}</mark>{text.slice(reference.end_codepoint).join("")}</pre>
    </> : <p role="alert">{t("gateway.diagnostics.locationMissingReference")}</p>}
  </section>;
}
