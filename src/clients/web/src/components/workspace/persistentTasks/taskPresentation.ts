const labels: Record<string, string> = {
  active: "已启用", paused: "已暂停", complete: "已完成", terminated: "已终止", needs_attention: "需要处理",
  waiting_trigger: "等待触发", recovering: "恢复中", pending: "等待检查", checking: "正在判断", delivering: "正在投递",
  delivered: "已投递", checked: "检查已结束", failed: "失败", suppressed: "未投递（状态已变化）", skipped: "已跳过",
  wait: "条件未满足", proceed: "条件满足", blocked: "已阻塞", budget_limited: "预算已用尽",
  manual: "手动", interval: "固定间隔", cron: "Cron", external: "外部通知", execution_end: "工作执行结束",
  completed: "已完成", completed_empty: "已完成（无输出）", cancelled: "已取消", interrupted: "已中断",
  unknown: "结果未知或仍在执行",
};

export const taskLabel = (value: string) => labels[value] ?? value;
