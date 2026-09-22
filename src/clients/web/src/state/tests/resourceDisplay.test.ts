import { describe, expect, test } from "bun:test";
import type { SessionResource } from "../../types/backend";
import {
  resourceKindIcon,
  resourceTreeTitle,
  stripTerminalNamePrefix,
} from "../display/resourceDisplay";

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
    // 归一后为空或等于 resource_id 时，回退到 cwd / 默认文案。
    expect(resourceTreeTitle(terminalResource("终端 /", { cwd: "/workspace/app" })))
      .toBe("终端 · app");
    expect(resourceTreeTitle(terminalResource("", {}))).toBe("用户终端");
  });

  test("资源种类图标为唯一映射", () => {
    expect(resourceKindIcon("browser")).toBe("codicon-globe");
    expect(resourceKindIcon("terminal")).toBe("codicon-terminal");
    expect(resourceKindIcon("background_task")).toBe("codicon-server-process");
  });
});
