import type { WebUiMainAreaRatios } from "../types/backend";

export type LayoutResizeTarget =
  | "agent-sessions-right"
  | "preview-left"
  | "auxiliary-left";

export type MainAreaKey = keyof WebUiMainAreaRatios;

export const DEFAULT_MAIN_AREA_RATIOS: WebUiMainAreaRatios = {
  agent_sessions: 1,
  chat: 1,
  workspace_preview: 1,
  auxiliary: 1,
};

export const LAYOUT_RESIZING_CLASS = "is-layout-resizing";

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
  if (typeof window === "undefined") {
    return true;
  }
  return window.innerWidth > 900;
}
