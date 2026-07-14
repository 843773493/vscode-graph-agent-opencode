import {
  DEFAULT_MAIN_AREA_RATIOS,
  resizeAdjacentMainAreas,
  resolveMainAreaRatios,
} from "../../layout/workbenchLayout";

const defaults = resolveMainAreaRatios(null);
if (JSON.stringify(defaults) !== JSON.stringify(DEFAULT_MAIN_AREA_RATIOS)) {
  throw new Error("主页四区默认比例不是 1:1:1:1");
}

const resized = resizeAdjacentMainAreas({
  ratios: defaults,
  left: "chat",
  right: "workspace_preview",
  leftWidth: 400,
  rightWidth: 400,
  deltaX: 100,
});
if (resized.chat !== 1.25 || resized.workspace_preview !== 0.75) {
  throw new Error(`相邻区域比例调整错误: ${JSON.stringify(resized)}`);
}
if (resized.agent_sessions !== 1 || resized.auxiliary !== 1) {
  throw new Error("拖拽修改了非相邻区域比例");
}

const rejected = resizeAdjacentMainAreas({
  ratios: defaults,
  left: "chat",
  right: "workspace_preview",
  leftWidth: 400,
  rightWidth: 400,
  deltaX: 500,
});
if (rejected !== defaults) {
  throw new Error("拖拽越过相邻区域边界时不应产生负比例");
}
