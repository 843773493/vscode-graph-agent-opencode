import { t } from "../../../../shared/core/i18n/index.js";
import type { GatewayDiagnosticLog } from "../../types/backend";

export function diagnosticLogStatusLabel(log: GatewayDiagnosticLog): string {
  if (log.status === "available") return t("gateway.diagnostics.logAvailable");
  if (log.status === "empty") return t("gateway.diagnostics.logEmpty");
  return t("gateway.diagnostics.logUnavailable");
}

export function diagnosticLogUnavailableHint(log: GatewayDiagnosticLog): string {
  const detail = log.error ?? t("gateway.diagnostics.gatewayNoLogContent");
  return t("gateway.diagnostics.logUnavailableHint", detail);
}
