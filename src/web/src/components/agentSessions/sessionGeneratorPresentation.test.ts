import { describe, expect, test } from "bun:test";

import type { GatewayWorkspace, SessionGeneratorDefinition } from "../../types/backend";
import {
  generatorStatusPresentation,
  generatorStrategyLabel,
  generatorTriggerLabel,
} from "./sessionGeneratorPresentation";

function remoteWorkspace(): GatewayWorkspace {
  return {
    workspace_id: "gw_remote_projection",
    parent_workspace_id: null,
    name: "home",
    root_path: "/home/remote",
    backend_url: "http://127.0.0.1:9000",
    connection_kind: "remote_gateway",
    status: "offline",
    active: false,
    managed: true,
    removable: true,
    system_default: false,
    runtime_action: "reconnect_remote_gateway",
    remote: {
      gateway_connection_id: "rgw_test",
      remote_workspace_id: "gw_remote",
      gateway_id: "gateway_test",
      name: "GPU Gateway",
      host: "10.0.0.8",
      port: 22,
      username: "developer",
      ssh_config_host: "gpu-dev",
      remote_gateway_port: 8014,
    },
    services: {},
    connection_error: "LookupError: 隧道尚未连接",
    checked_at: "2026-07-31T00:00:00Z",
  };
}

function blockedGenerator(): SessionGeneratorDefinition {
  return {
    generator_id: "gen_test",
    name: "UX Interval Continue",
    enabled: true,
    status: "blocked",
    status_reason: "生成目标不可用: LookupError: Gateway 隧道不存在",
    revision: 1,
    trigger: {
      type: "interval",
      expression: null,
      interval_seconds: 86400,
      timezone: "UTC",
    },
    placement: { kind: "workspace", workspace_id: "gw_remote_projection" },
    execution_workspace_id: "gw_remote_projection",
    session_strategy: {
      mode: "continue_existing",
      target: { workspace_id: "gw_remote_projection", session_id: "ses_test" },
      concurrency: "queue",
      report_back: "none",
    },
    naming: { title_template: "{generator.name}", path_template: [] },
    config: {},
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
  };
}

describe("会话生成器展示", () => {
  test("策略枚举转换为面向用户的文字", () => {
    expect(generatorStrategyLabel("new_per_run")).toBe("每次创建新会话");
    expect(generatorStrategyLabel("continue_existing")).toBe("继续现有会话");
    expect(generatorStrategyLabel("fork_new_and_report_back")).toBe("创建分支并回报结果");
  });

  test("触发配置转换为紧凑时间说明", () => {
    expect(generatorTriggerLabel({
      type: "interval",
      interval_seconds: 3600,
      timezone: "UTC",
    })).toBe("每 1 小时");
    expect(generatorTriggerLabel({
      type: "manual",
      timezone: "UTC",
    })).toBe("仅手动运行");
  });

  test("远程目标阻塞时隐藏内部异常并给出恢复摘要", () => {
    const generator = blockedGenerator();

    expect(generatorStatusPresentation(generator, remoteWorkspace())).toEqual({
      label: "需要处理",
      tone: "blocked",
      title: "生成目标不可用",
      message: "远程 Gateway“gpu-dev”当前未连接。",
      technicalDetail: generator.status_reason ?? null,
    });
  });

  test("目标工作区丢失时引导用户进入连接管理", () => {
    const generator: SessionGeneratorDefinition = {
      ...blockedGenerator(),
      status: "blocked",
      status_reason: "LookupError: Gateway 工作区不存在: gw_missing",
    };

    expect(generatorStatusPresentation(generator, undefined)).toEqual({
      label: "需要处理",
      tone: "blocked",
      title: "生成目标不可用",
      message: "目标工作区已不存在或尚未连接，请先在连接管理中恢复该工作区。",
      technicalDetail: generator.status_reason ?? null,
    });
  });
});
