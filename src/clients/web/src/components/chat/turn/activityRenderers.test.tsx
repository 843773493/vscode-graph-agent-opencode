import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ActivityRendererRegistry,
  activityRendererRegistry,
} from "./activityRenderers";
import type { MessageStreamActivity } from "../../../state/messageStream";

function activity(
  kind: string,
  status: MessageStreamActivity["status"] = "running",
): MessageStreamActivity {
  return {
    activity_id: "activity_1",
    kind,
    scope_ref: "turn",
    status,
    cancellable: false,
    resumable: false,
    side_effect_policy: "unknown",
    resource_refs: [],
    detail_available: false,
  };
}

describe("Activity Renderer", () => {
  test("未知 kind 使用通用状态，不投影成模型文本或工具", () => {
    const result = activityRendererRegistry.render(activity("provider.private"));
    expect(result).toBeTruthy();
  });

  test("未知 Activity 的所有生命周期状态都有明确文案", () => {
    const labels = [
      ["running", "正在处理 provider.private"],
      ["waiting", "等待继续 provider.private"],
      ["stopping", "正在停止 provider.private"],
      ["completed", "已完成 provider.private"],
      ["failed", "处理失败 provider.private"],
      ["unknown", "结果未知 provider.private"],
    ] as const;
    for (const [status, label] of labels) {
      const html = renderToStaticMarkup(
        <>{activityRendererRegistry.render(activity("provider.private", status))}</>,
      );
      expect(html).toContain(label);
    }
  });

  test("允许注册新的 detail-aware renderer，但拒绝重复 kind", () => {
    const registry = new ActivityRendererRegistry();
    registry.register("custom.activity", (item) => item.summary ?? "custom");
    expect(registry.render(activity("custom.activity"))).toBe("custom");
    expect(() => registry.register("custom.activity", () => "duplicate")).toThrow(
      "重复注册",
    );
  });

  test("压缩 Activity 的生命周期状态使用明确文案", () => {
    const labels = [
      ["running", "正在压缩上下文"],
      ["waiting", "等待上下文压缩继续"],
      ["stopping", "正在完成上下文压缩"],
      ["completed", "上下文压缩已完成"],
      ["failed", "上下文压缩失败"],
      ["unknown", "上下文压缩结果未知"],
    ] as const;
    for (const [status, label] of labels) {
      const html = renderToStaticMarkup(
        <>{activityRendererRegistry.render(activity("context.compaction", status))}</>,
      );
      expect(html).toContain(label);
    }
  });

  test("压缩 Activity 的服务端 summary 不覆盖生命周期结果", () => {
    const item = activity("context.compaction", "failed");
    item.summary = "压缩请求已返回服务端错误";
    const html = renderToStaticMarkup(
      <>{activityRendererRegistry.render(item)}</>,
    );

    expect(html).toContain("上下文压缩失败");
    expect(html).toContain("压缩请求已返回服务端错误");
  });

  test("审批、子 Agent 和资源 Activity 不会把终态显示成运行中", () => {
    const cases = [
      ["approval.wait", "审批已完成"],
      ["subagent.run", "子 Agent 执行失败"],
      ["resource.operation", "工作区资源操作结果未知"],
    ] as const;
    for (const [kind, label] of cases) {
      const status = label.endsWith("未知") ? "unknown" : label.includes("失败") ? "failed" : "completed";
      const html = renderToStaticMarkup(
        <>{activityRendererRegistry.render(activity(kind, status))}</>,
      );
      expect(html).toContain(label);
      expect(html).not.toContain("正在运行");
    }
  });

  test("可恢复的资源失败显示明确恢复动作", () => {
    const item = activity("resource.operation", "failed");
    item.detail_available = true;
    item.detail = {
      operation: "runPlaywrightCode",
      code: "browser_tool_timeout",
      retryable: true,
      recovery: "page_reset",
    };
    const html = renderToStaticMarkup(
      <>{activityRendererRegistry.render(item)}</>,
    );

    expect(html).toContain("浏览器操作失败");
    expect(html).toContain("页面已重置，请重新读取页面后重试");
  });
});
