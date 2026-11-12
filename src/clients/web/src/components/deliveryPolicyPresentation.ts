import { t } from "../../../shared/core/i18n/index.js";
import type { DeliveryPolicy } from "../types/backend";

/** 投递边界文案与顺序的唯一来源；Composer、待处理队列条与操作条共用。 */
export const DELIVERY_POLICY_LABELS: Record<DeliveryPolicy, string> = {
  after_turn: t("delivery.afterTurn"),
  after_tool_result: t("delivery.afterTool"),
  after_interrupt: t("delivery.afterInterrupt"),
};

export const DELIVERY_POLICIES = Object.keys(
  DELIVERY_POLICY_LABELS,
) as DeliveryPolicy[];
