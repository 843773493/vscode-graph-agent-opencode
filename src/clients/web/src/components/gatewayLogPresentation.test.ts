import { describe, expect, test } from "bun:test";
import type { GatewayDiagnosticLog } from "../types/backend";
import {
  diagnosticLogStatusLabel,
  diagnosticLogUnavailableHint,
} from "./gatewayLogPresentation";

function log(status: string, error?: string): GatewayDiagnosticLog {
  return {
    log_id: "workspace:test:terminal_manager",
    source: "workspace",
    workspace_id: "gw_test",
    workspace_name: "测试工作区",
    service: "terminal_manager",
    label: "测试工作区 · Terminal Manager",
    status,
    error,
  } as GatewayDiagnosticLog;
}

describe("诊断日志状态展示", () => {
  test("独立日志不可用不投影为服务离线", () => {
    const unavailable = log("unavailable", "日志文件不存在: local-terminal-55579.log");

    expect(diagnosticLogStatusLabel(unavailable)).toBe("日志不可用");
    expect(diagnosticLogUnavailableHint(unavailable)).toContain("不代表对应工作区服务已离线");
    expect(diagnosticLogUnavailableHint(unavailable)).toContain("local-terminal-55579.log");
  });

  test("保留可读和空日志状态", () => {
    expect(diagnosticLogStatusLabel(log("available"))).toBe("可读");
    expect(diagnosticLogStatusLabel(log("empty"))).toBe("空日志");
  });
});
