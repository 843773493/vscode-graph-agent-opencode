import { describe, expect, test } from "bun:test";
import type { SessionResource } from "../../types/backend";
import {
  resourceAttentionGroup,
  resourceKindIcon,
  resourceMetadataRows,
  resourceTreeStatus,
  resourceTreeTitle,
  statusLabel,
  stripTerminalNamePrefix,
} from "./resourceDisplay";

function terminalResource(
  name: string,
  metadata: Record<string, unknown> = {},
): SessionResource {
  return {
    resource_id: "terminal_01",
    session_id: "ses_resource_display",
    kind: "terminal",
    name,
    status: "running",
    created_at: "2026-09-15T12:00:00Z",
    updated_at: "2026-09-15T12:05:00Z",
    started_at: null,
    ended_at: null,
    available_actions: ["cancel", "delete"],
    metadata,
  };
}

describe("资源展示投影", () => {
  test("终端名「终端 / 前缀」只有唯一归一实现，面板与树行口径一致", () => {
    // 归一实现只裁前缀（含前缀后的空白），不负责裁名字自身的尾随空白；
    // 需要 trim 的调用方（resourceTreeTitle）自行 trim，保持原两条调用链语义一致。
    expect(stripTerminalNamePrefix("终端 / 主终端")).toBe("主终端");
    expect(stripTerminalNamePrefix("终端/主终端")).toBe("主终端");
    expect(stripTerminalNamePrefix("终端 /  主终端  ")).toBe("主终端  ");
    // 无前缀名字必须原样保留，不得误裁。
    expect(stripTerminalNamePrefix("普通终端")).toBe("普通终端");

    // resourceTreeTitle 走同一实现：前缀裁掉后直接作为标题。
    expect(resourceTreeTitle(terminalResource("终端 / 主终端"))).toBe("主终端");
    // 归一后为空或等于 resource_id 时，回退到工作区内相对目录 / 默认文案。
    expect(resourceTreeTitle(terminalResource("终端 /", { cwd_relative: "app" })))
      .toBe("终端 · app");
    expect(resourceTreeTitle(terminalResource("", {}))).toBe("用户终端");
  });

  test("资源种类图标为唯一映射", () => {
    expect(resourceKindIcon("browser")).toBe("codicon-globe");
    expect(resourceKindIcon("terminal")).toBe("codicon-terminal");
    expect(resourceKindIcon("background_task")).toBe("codicon-server-process");
  });

  test("资源详情只生成可见元数据行，保留属性顺序与原值格式", () => {
    const resource = terminalResource("终端", {
      cwd_relative: "app",
      attach_url: "不应展示",
      command_exit_code: 7,
      custom_value: { count: 2 },
    });

    expect(resourceMetadataRows(resource)).toEqual([
      ["工作目录", "app"],
      ["退出码", "7"],
      ["custom_value", '{"count":2}'],
    ]);
  });

  test("资源分组状态映射保持失败、历史、挂起和可恢复边界", () => {
    const failed = terminalResource("失败");
    failed.status = "failed";
    const historical = terminalResource("历史");
    historical.status = "closed";
    const sleeping = terminalResource("挂起", { resource_state: "frozen" });
    const backgroundTask: SessionResource = {
      ...terminalResource("后台任务"),
      kind: "background_task",
      status: "pending",
    };
    const recoverableBrowser: SessionResource = {
      ...terminalResource("可恢复浏览器"),
      kind: "browser",
      status: "lost",
      metadata: { resource_state: "discarded", checkpoint_available: true },
    };

    expect(resourceAttentionGroup(failed, null)).toBe("attention");
    expect(resourceAttentionGroup(historical, null)).toBe("history");
    expect(resourceAttentionGroup(sleeping, null)).toBe("sleeping");
    expect(resourceAttentionGroup(backgroundTask, "terminal://another"))
      .toBe("active");
    expect(resourceAttentionGroup(recoverableBrowser, null)).toBe("sleeping");
  });

  test("资源状态只格式化一次连接数并显示用户可读值", () => {
    expect(resourceTreeStatus(terminalResource("终端", { client_count: 3 })))
      .toBe("3 个连接");
  });

  test("后端实际会发出的资源状态都有中文标签，不把英文枚举漏给用户", () => {
    // 终端 provider 会发 created/exited，浏览器 provider 会发 frozen/discarded，
    // 两者在记录缺失时都会回退 unknown。
    expect(statusLabel("created")).toBe("已创建");
    expect(statusLabel("exited")).toBe("已退出");
    expect(statusLabel("frozen")).toBe("已冻结");
    expect(statusLabel("discarded")).toBe("已冷回收");
    expect(statusLabel("unknown")).toBe("状态未知");
  });
});
