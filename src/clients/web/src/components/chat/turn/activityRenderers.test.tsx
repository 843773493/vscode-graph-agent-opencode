import { describe, expect, test } from "bun:test";
import {
  ActivityRendererRegistry,
  activityRendererRegistry,
} from "./activityRenderers";
import type { MessageStreamActivity } from "../../../state/messageStream";

function activity(kind: string): MessageStreamActivity {
  return {
    activity_id: "activity_1",
    kind,
    scope_ref: "turn",
    status: "running",
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

  test("允许注册新的 detail-aware renderer，但拒绝重复 kind", () => {
    const registry = new ActivityRendererRegistry();
    registry.register("custom.activity", (item) => item.summary ?? "custom");
    expect(registry.render(activity("custom.activity"))).toBe("custom");
    expect(() => registry.register("custom.activity", () => "duplicate")).toThrow(
      "重复注册",
    );
  });
});
