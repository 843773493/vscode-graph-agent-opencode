import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type PointerEvent as ReactPointerEvent,
  type SetStateAction,
} from "react";
import {
  DEFAULT_GATEWAY_PANEL_HEIGHT,
  GATEWAY_PANEL_RESIZING_CLASS,
  clampGatewayPanelHeight,
} from "../../layout/workbenchLayout";
import type { WorkspaceBottomPanelState } from "../../state/workspaceBottomPanel";
import { installPointerDrag } from "../../utils/pointerDrag";

interface UseBottomPanelResizeOptions {
  workspaceId: string | null;
  panelState: WorkspaceBottomPanelState;
  setWorkspaceBottomPanelStates: Dispatch<
    SetStateAction<Record<string, WorkspaceBottomPanelState>>
  >;
  updateBottomPanelState: (patch: Partial<WorkspaceBottomPanelState>) => void;
}

/**
 * 管理主窗口底部面板的纵向拖拽与高度还原。
 *
 * 拖拽过程中只更新工作区本地面板状态，松手后才通过
 * `updateBottomPanelState` 落盘高度，保持“后端是持久化权威”的既有行为。
 */
export function useBottomPanelResize({
  workspaceId,
  panelState,
  setWorkspaceBottomPanelStates,
  updateBottomPanelState,
}: UseBottomPanelResizeOptions) {
  const cleanupResizeRef = useRef<(() => void) | null>(null);

  useEffect(() => () => {
    cleanupResizeRef.current?.();
  }, []);

  const startBottomPanelResize = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      cleanupResizeRef.current?.();

      const startY = event.clientY;
      const startHeight = panelState.height;
      let latestHeight = startHeight;
      let moved = false;

      const handlePointerMove = (moveEvent: PointerEvent) => {
        const deltaY = startY - moveEvent.clientY;
        if (deltaY === 0) {
          return;
        }
        moved = true;
        latestHeight = clampGatewayPanelHeight(startHeight + deltaY);
        if (workspaceId) {
          setWorkspaceBottomPanelStates((previous) => ({
            ...previous,
            [workspaceId]: { ...panelState, height: latestHeight },
          }));
        }
      };

      cleanupResizeRef.current = installPointerDrag(
        GATEWAY_PANEL_RESIZING_CLASS,
        handlePointerMove,
        () => {
          cleanupResizeRef.current = null;
          if (moved) {
            updateBottomPanelState({ height: latestHeight });
          }
        },
      );
    },
    [
      panelState,
      setWorkspaceBottomPanelStates,
      updateBottomPanelState,
      workspaceId,
    ],
  );

  const resetBottomPanelHeight = useCallback(
    () => updateBottomPanelState({ height: DEFAULT_GATEWAY_PANEL_HEIGHT }),
    [updateBottomPanelState],
  );

  return { resetBottomPanelHeight, startBottomPanelResize };
}
