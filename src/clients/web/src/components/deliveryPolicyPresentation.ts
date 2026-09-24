import type { DeliveryPolicy } from "../types/backend";

/** 投递边界文案与顺序的唯一来源；Composer、待处理队列条与操作条共用。 */
export const DELIVERY_POLICY_LABELS: Record<DeliveryPolicy, string> = {
  after_turn: "本轮结束后投递",
  after_tool_result: "工具结果后投递",
  after_interrupt: "中断边界后投递",
};

export const DELIVERY_POLICIES = Object.keys(
  DELIVERY_POLICY_LABELS,
) as DeliveryPolicy[];
