import {
  autoUpdate,
  flip,
  FloatingPortal,
  offset as floatingOffset,
  shift,
  size,
  type VirtualElement,
  useDismiss,
  useFloating,
  useInteractions,
} from "@floating-ui/react";
import React, { useEffect, useLayoutEffect, useMemo } from "react";

import type { AnchoredOverlayProps, OverlayPoint } from "./AnchoredOverlay";

const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

function pointReference(point: OverlayPoint): VirtualElement {
  return {
    getBoundingClientRect: () => ({
      x: point.x,
      y: point.y,
      top: point.y,
      right: point.x,
      bottom: point.y,
      left: point.x,
      width: 0,
      height: 0,
      toJSON: () => undefined,
    }),
  };
}

/**
 * 将锚点浮层提升到 document.body，避免被工作台分栏的 overflow 裁切。
 * Floating UI 负责翻转、视口边界移动、可用尺寸约束和布局变化跟随。
 */
export default function AnchoredOverlayFloating({
  open,
  anchorRef,
  point,
  placement = "bottom-start",
  offset = 6,
  viewportPadding = 8,
  dismissible = true,
  onClose,
  children,
}: AnchoredOverlayProps): React.ReactNode {
  const virtualReference = useMemo(
    () => (point ? pointReference(point) : null),
    [point],
  );
  const middleware = useMemo(
    () => [
      floatingOffset(offset),
      flip({ padding: viewportPadding }),
      shift({ padding: viewportPadding }),
      size({
        padding: viewportPadding,
        apply({ availableWidth, availableHeight, elements }) {
          elements.floating.style.setProperty(
            "--anchored-overlay-available-width",
            `${Math.max(0, availableWidth)}px`,
          );
          elements.floating.style.setProperty(
            "--anchored-overlay-available-height",
            `${Math.max(0, availableHeight)}px`,
          );
        },
      }),
    ],
    [offset, viewportPadding],
  );
  const { refs, floatingStyles, context, isPositioned, update } = useFloating({
    open,
    onOpenChange: (nextOpen) => {
      if (!nextOpen) {
        onClose();
      }
    },
    placement,
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware,
  });
  const dismiss = useDismiss(context, {
    enabled: dismissible,
    escapeKey: true,
    outsidePress: true,
  });
  const { getFloatingProps } = useInteractions([dismiss]);

  useIsomorphicLayoutEffect(() => {
    if (virtualReference) {
      refs.setPositionReference(virtualReference);
      if (open) {
        void update();
      }
      return;
    }
    const reference = anchorRef?.current ?? null;
    refs.setReference(reference);
    if (open && reference) {
      void update();
    }
  }, [anchorRef, open, refs, update, virtualReference]);

  return (
    <FloatingPortal>
      <div
        ref={refs.setFloating}
        className="anchored-overlay-positioner"
        style={{
          ...floatingStyles,
          visibility: isPositioned ? "visible" : "hidden",
        }}
        {...getFloatingProps()}
      >
        {children}
      </div>
    </FloatingPortal>
  );
}
