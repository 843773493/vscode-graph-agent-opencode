import type {
  GatewayWorkspace,
  GeneratorSessionStrategyMode,
  SessionGeneratorDefinition,
} from "../../types/backend";

export interface GeneratorStatusPresentation {
  label: string;
  tone: "ready" | "paused" | "blocked";
  title: string | null;
  message: string | null;
  technicalDetail: string | null;
}

const STRATEGY_LABELS: Record<GeneratorSessionStrategyMode, string> = {
  new_per_run: "每次创建新会话",
  continue_existing: "继续现有会话",
  fork_new_and_report_back: "创建分支并回报结果",
};

export function generatorStrategyLabel(mode: GeneratorSessionStrategyMode): string {
  return STRATEGY_LABELS[mode];
}

export function generatorTriggerLabel(
  trigger: SessionGeneratorDefinition["trigger"],
): string {
  if (trigger.type === "manual") return "仅手动运行";
  if (trigger.type === "cron") {
    return `Cron：${trigger.expression ?? "未配置"} · ${trigger.timezone}`;
  }
  const seconds = trigger.interval_seconds ?? 0;
  if (seconds > 0 && seconds % 86400 === 0) return `每 ${seconds / 86400} 天`;
  if (seconds > 0 && seconds % 3600 === 0) return `每 ${seconds / 3600} 小时`;
  if (seconds > 0 && seconds % 60 === 0) return `每 ${seconds / 60} 分钟`;
  return `每 ${seconds} 秒`;
}

function remoteGatewayName(workspace: GatewayWorkspace): string {
  return workspace.remote?.ssh_config_host
    ?? workspace.remote?.name
    ?? workspace.remote?.host
    ?? "远程 Gateway";
}

export function generatorStatusPresentation(
  generator: SessionGeneratorDefinition,
  targetWorkspace: GatewayWorkspace | undefined,
): GeneratorStatusPresentation {
  if (generator.status === "ready") {
    return {
      label: "就绪",
      tone: "ready",
      title: null,
      message: null,
      technicalDetail: null,
    };
  }
  if (generator.status === "paused") {
    return {
      label: "已暂停",
      tone: "paused",
      title: "生成器已暂停",
      message: "该生成器当前不会自动运行。",
      technicalDetail: generator.status_reason ?? null,
    };
  }

  let message = "目标工作区已不存在或尚未连接，请先在连接管理中恢复该工作区。";
  if (targetWorkspace?.connection_kind === "remote_gateway") {
    message = `远程 Gateway“${remoteGatewayName(targetWorkspace)}”当前未连接。`;
  } else if (targetWorkspace?.status === "offline" && targetWorkspace.managed) {
    message = `目标工作区“${targetWorkspace.name}”当前已停止。`;
  } else if (targetWorkspace) {
    message = `目标工作区“${targetWorkspace.name}”当前不可用。`;
  }
  return {
    label: "需要处理",
    tone: "blocked",
    title: "生成目标不可用",
    message,
    technicalDetail: generator.status_reason ?? null,
  };
}
