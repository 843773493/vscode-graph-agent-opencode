import React, { lazy, Suspense } from "react";
import type { Placement } from "@floating-ui/react";

export interface OverlayPoint {
  x: number;
  y: number;
}

export interface AnchoredOverlayProps {
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

const PositionedAnchoredOverlay = lazy(
  () => import("./AnchoredOverlayFloating"),
);

/**
 * 轻量浮层入口：默认关闭时不加载 Floating UI，把定位实现延迟到首次打开。
 * 无 document 的渲染环境继续直接输出内容，不触发浏览器定位代码。
 */
export default function AnchoredOverlay(
  props: AnchoredOverlayProps,
): React.ReactNode {
  if (!props.open) {
    return null;
  }
  if (typeof document === "undefined") {
    return <>{props.children}</>;
  }
  return (
    <Suspense fallback={null}>
      <PositionedAnchoredOverlay {...props} />
    </Suspense>
  );
}
