import { useContext } from "react";
import { OverlayRootContext } from "./OverlayRootContext";
import {
  autoUpdate,
  flip,
  FloatingFocusManager,
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
 * 将锚点浮层提升到所属宿主的 Portal 根，避免被工作台分栏的 overflow 裁切。
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
  manageFocus = false,
  onClose,
  children,
}: AnchoredOverlayProps): React.ReactNode {
  const overlayRoot = useContext(OverlayRootContext);
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
    escapeKey: !manageFocus,
    outsidePress: true,
  });
  const { getFloatingProps } = useInteractions([dismiss]);

  useEffect(() => {
    const floating = refs.floating.current;
    if (!open || !dismissible || !manageFocus || !floating) return;
    // 原生模态编辑器在捕获阶段处理 Esc；先关闭拥有焦点的浮层，不能连带关闭编辑器。
    const onKeyDown = (event: KeyboardEvent) => {
      const path = event.composedPath();
      const reference = refs.domReference.current;
      if (event.key !== 'Escape' || event.isComposing
        || !path.includes(floating) && !(reference && path.includes(reference))) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    const owner = floating.ownerDocument;
    owner.addEventListener('keydown', onKeyDown, true);
    return () => owner.removeEventListener('keydown', onKeyDown, true);
  }, [open, dismissible, manageFocus, onClose, refs]);

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
    <FloatingPortal root={overlayRoot ?? undefined}>
      <FloatingFocusManager context={context} modal={false} disabled={!manageFocus || !isPositioned}>
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
      </FloatingFocusManager>
    </FloatingPortal>
  );
}
