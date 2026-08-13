import type { WebUiMainAreaRatios } from "../types/backend";

export type LayoutResizeTarget =
  | "agent-sessions-right"
  | "workspace-editor-left"
  | "auxiliary-left";

export type MainAreaKey = keyof WebUiMainAreaRatios;

export const DEFAULT_MAIN_AREA_RATIOS: WebUiMainAreaRatios = {
  agent_sessions: 1,
  // Codex 工作台将会话区与编辑器工作区并列，编辑器工作区内部再平分文档和文件树。
  chat: 1,
  workspace_preview: 1,
  auxiliary: 1,
};

export const LAYOUT_RESIZING_CLASS = "is-layout-resizing";
export const GATEWAY_PANEL_RESIZING_CLASS = "is-gateway-panel-resizing";
export const DEFAULT_GATEWAY_PANEL_HEIGHT = 286;
export const MIN_GATEWAY_PANEL_HEIGHT = 190;
export const MAX_GATEWAY_PANEL_HEIGHT = 520;

export function clampGatewayPanelHeight(value: number): number {
  return Math.min(
    MAX_GATEWAY_PANEL_HEIGHT,
    Math.max(MIN_GATEWAY_PANEL_HEIGHT, Math.round(value)),
  );
}

export function resolveMainAreaRatios(
  value: WebUiMainAreaRatios | null | undefined,
): WebUiMainAreaRatios {
  if (!value) {
    return { ...DEFAULT_MAIN_AREA_RATIOS };
  }
  for (const ratio of Object.values(value)) {
    if (!Number.isFinite(ratio) || ratio <= 0) {
      throw new Error(`主页区域比例必须是正数: ${JSON.stringify(value)}`);
    }
  }
  return { ...value };
}

export function resizeAdjacentMainAreas({
  ratios,
  left,
  right,
  leftWidth,
  rightWidth,
  deltaX,
}: {
  ratios: WebUiMainAreaRatios;
  left: MainAreaKey;
  right: MainAreaKey;
  leftWidth: number;
  rightWidth: number;
  deltaX: number;
}): WebUiMainAreaRatios {
  const combinedWidth = leftWidth + rightWidth;
  if (combinedWidth <= 0) {
    throw new Error(`无法调整没有宽度的主页区域: left=${left}, right=${right}`);
  }

  const nextLeftWidth = leftWidth + deltaX;
  const nextRightWidth = rightWidth - deltaX;
  if (nextLeftWidth <= 0 || nextRightWidth <= 0) {
    return ratios;
  }

  const combinedRatio = ratios[left] + ratios[right];
  return {
    ...ratios,
    [left]: combinedRatio * (nextLeftWidth / combinedWidth),
    [right]: combinedRatio * (nextRightWidth / combinedWidth),
  };
}

export function defaultAuxiliaryVisible(): boolean {
  return true;
}
