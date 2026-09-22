import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type PointerEvent as ReactPointerEvent,
  type SetStateAction,
} from "react";
import {
  DEFAULT_EXTENSION_DEBUG_AREA_RATIOS,
  DEFAULT_MAIN_AREA_RATIOS,
  LAYOUT_RESIZING_CLASS,
  resizeAdjacentMainAreas,
  type LayoutResizeTarget,
  type MainAreaKey,
} from "../../layout/workbenchLayout";
import type {
  WebUiMainAreaRatios,
  WebUiSettingsUpdate,
} from "../../types/backend";
import { installPointerDrag } from "../../utils/pointerDrag";

type ExtensionDebugAreaRatios = Pick<
  WebUiMainAreaRatios,
  "workspace_preview" | "auxiliary"
>;

type RatioSetter<T> = Dispatch<SetStateAction<T>>;

interface UseMainAreaResizeOptions {
  mainAreaRatios: WebUiMainAreaRatios;
  setMainAreaRatios: RatioSetter<WebUiMainAreaRatios>;
  persistLayoutSettings: (layout: WebUiSettingsUpdate["layout"]) => void;
  extensionDebugSplitActive: boolean;
}

const RESIZE_AREAS: Record<LayoutResizeTarget, readonly [MainAreaKey, string, MainAreaKey, string]> = {
  "agent-sessions-right": ["agent_sessions", ".agent-sessions-panel", "chat", ".workbench-main-column"],
  "workspace-editor-left": ["chat", ".sessions-part-card", "workspace_preview", ".workspace-editor-shell"],
  "auxiliary-left": ["workspace_preview", ".workspace-preview-panel", "auxiliary", ".auxiliary-panel"],
};

export function useMainAreaResize({
  mainAreaRatios,
  setMainAreaRatios,
  persistLayoutSettings,
  extensionDebugSplitActive,
}: UseMainAreaResizeOptions) {
  const [extensionDebugAreaRatios, setExtensionDebugAreaRatios] = useState(
    () => ({ ...DEFAULT_EXTENSION_DEBUG_AREA_RATIOS }),
  );
  const cleanupResizeRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => {
      cleanupResizeRef.current?.();
    };
  }, []);

  const startLayoutResize = useCallback(
    (
      target: LayoutResizeTarget,
      event: ReactPointerEvent<HTMLButtonElement>,
    ) => {
      event.preventDefault();
      cleanupResizeRef.current?.();

      const startX = event.clientX;
      const resizingExtensionDebugSplit = target === "auxiliary-left" && extensionDebugSplitActive;
      const startRatios = resizingExtensionDebugSplit
        ? {
            ...mainAreaRatios,
            workspace_preview: extensionDebugAreaRatios.workspace_preview,
            auxiliary: extensionDebugAreaRatios.auxiliary,
          }
        : mainAreaRatios;
      const [left, leftSelector, right, rightSelector] = RESIZE_AREAS[target];
      const grouped: readonly MainAreaKey[] = target === "agent-sessions-right"
        ? ["workspace_preview", "auxiliary"]
        : target === "workspace-editor-left" ? ["auxiliary"] : [];
      const leftArea = document.querySelector<HTMLElement>(leftSelector);
      const rightArea = document.querySelector<HTMLElement>(rightSelector);
      if (!leftArea || !rightArea) {
        throw new Error(
          `主页布局区域不存在: left=${leftSelector}, right=${rightSelector}`,
        );
      }
      const leftWidth = leftArea.getBoundingClientRect().width;
      const rightWidth = rightArea.getBoundingClientRect().width;
      let latestRatios = startRatios;
      let moved = false;

      const handlePointerMove = (moveEvent: PointerEvent) => {
        const deltaX = moveEvent.clientX - startX;
        if (deltaX === 0) {
          return;
        }
        moved = true;
        latestRatios = resizeAdjacentMainAreas({
          ratios: startRatios,
          left,
          right,
          grouped,
          leftWidth,
          rightWidth,
          deltaX,
        });
        if (resizingExtensionDebugSplit) {
          setExtensionDebugAreaRatios({
            workspace_preview: latestRatios.workspace_preview,
            auxiliary: latestRatios.auxiliary,
          });
        } else {
          setMainAreaRatios(latestRatios);
        }
      };

      cleanupResizeRef.current = installPointerDrag(
        LAYOUT_RESIZING_CLASS,
        handlePointerMove,
        () => {
          cleanupResizeRef.current = null;
          if (moved && !resizingExtensionDebugSplit) {
            persistLayoutSettings({ main_area_ratios: latestRatios });
          }
        },
      );
    },
    [
      extensionDebugAreaRatios,
      extensionDebugSplitActive,
      mainAreaRatios,
      persistLayoutSettings,
      setMainAreaRatios,
    ],
  );

  const resetExtensionDebugAreaRatios = useCallback(
    () => setExtensionDebugAreaRatios({ ...DEFAULT_EXTENSION_DEBUG_AREA_RATIOS }),
    [],
  );

  return {
    extensionDebugAreaRatios,
    resetExtensionDebugAreaRatios,
    startLayoutResize,
  };
}
