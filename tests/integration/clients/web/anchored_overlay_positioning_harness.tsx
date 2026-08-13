import React, { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import AnchoredOverlay from "../../../../src/clients/web/src/components/AnchoredOverlay";

function AnchoredOverlayHarness(): React.ReactNode {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);

  return (
    <main
      data-testid="layout"
      style={{
        minHeight: "100vh",
        background: "#f5f5f5",
        padding: "24px",
      }}
    >
      <p>AnchoredOverlay 定位回归测试</p>
      <button
        ref={anchorRef}
        type="button"
        data-testid="anchor"
        onClick={() => setOpen((current) => !current)}
        style={{
          position: "fixed",
          right: "96px",
          bottom: "72px",
          width: "176px",
          height: "44px",
        }}
      >
        打开模型选择器
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={anchorRef}
        placement="bottom-start"
        onClose={() => setOpen(false)}
      >
        <div
          data-testid="overlay"
          style={{
            width: "280px",
            minHeight: "160px",
            padding: "16px",
            background: "white",
            border: "1px solid #999",
            boxSizing: "border-box",
          }}
        >
          模型选项
        </div>
      </AnchoredOverlay>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<AnchoredOverlayHarness />);
