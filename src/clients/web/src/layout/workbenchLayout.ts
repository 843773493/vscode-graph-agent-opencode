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

export const DEFAULT_EXTENSION_DEBUG_AREA_RATIOS = {
  workspace_preview: 1.55,
  auxiliary: 0.85,
} satisfies Pick<WebUiMainAreaRatios, "workspace_preview" | "auxiliary">;

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

/** 会话与资源的分隔线始终停在可用边界，越界拖动不跳回起点。 */
export function resizeThreadResources({
  leftWidth,
  rightWidth,
  deltaX,
}: {
  leftWidth: number;
  rightWidth: number;
  deltaX: number;
}): number {
  const combinedWidth = leftWidth + rightWidth;
  if (combinedWidth < 520) {
    throw new Error(`会话与资源并排显示至少需要 520px，当前为 ${combinedWidth}px`);
  }
  const nextLeftWidth = Math.min(combinedWidth - 240, Math.max(280, leftWidth + deltaX));
  return (combinedWidth - nextLeftWidth) / combinedWidth;
}

export function defaultAuxiliaryVisible(): boolean {
  return true;
}
