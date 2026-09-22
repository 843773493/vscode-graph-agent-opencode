import {
  autoUpdate,
  flip,
  FloatingPortal,
  offset as floatingOffset,
  shift,
  size,
  type Placement,
  type VirtualElement,
  useDismiss,
  useFloating,
  useInteractions,
} from "@floating-ui/react";
import React, { useEffect, useLayoutEffect, useMemo } from "react";

const useIsomorphicLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

export interface OverlayPoint {
  x: number;
  y: number;
}

interface AnchoredOverlayProps {
  open: boolean;
  anchorRef?: React.RefObject<HTMLElement | null>;
  point?: OverlayPoint;
  placement?: Placement;
  offset?: number;
  viewportPadding?: number;
  dismissible?: boolean;
  onClose: () => void;
  children: React.ReactNode;
}

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
export default function AnchoredOverlay({
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
    middleware: [
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

  if (!open) {
    return null;
  }
  if (typeof document === "undefined") {
    return <>{children}</>;
  }

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
