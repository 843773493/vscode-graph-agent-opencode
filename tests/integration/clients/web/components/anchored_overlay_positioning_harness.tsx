import React, { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "@vscode/codicons/dist/codicon.css";
import AnchoredOverlay from "../../../../../src/clients/web/src/components/overlays/AnchoredOverlay";
import WorkspaceFileTree from "../../../../../src/clients/web/src/components/workspace/fileTree/WorkspaceFileTree";
import "../../../../../src/clients/web/src/styles/foundation/theme.css";
import "../../../../../src/clients/web/src/index.css";
import "../../../../../src/clients/web/src/styles/foundation/workbenchLayout.css";
import type { WorkspaceFileNode } from "../../../../../src/clients/web/src/types/backend";

const WORKSPACE_ROOT = "boxteam://workspace/backend_files/resources/files/root";
const ignoreExpandedPathsChange = (_paths: string[]) => {};
const ignoreSearchClose = () => {};
const ignoreFileOpen = (_node: WorkspaceFileNode) => {};

function AnchoredOverlayHarness(): React.ReactNode {
  const anchorRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [fileTreeStatus, setFileTreeStatus] = useState("");

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
        type="button"
        data-testid="open-file-tree-search"
        onClick={() => setSearchOpen(true)}
      >
        搜索文件树
      </button>
      <output
        data-testid="file-tree-status"
        style={{ display: "block", height: "24px", overflow: "hidden" }}
      >
        {fileTreeStatus}
      </output>
      <section
        aria-label="工作区文件树错误提示布局"
        style={{
          display: "flex",
          flexDirection: "column",
          width: "360px",
          height: "560px",
          border: "1px solid #999",
        }}
      >
        <WorkspaceFileTree
          active
          apiPort={8014}
          workspaceId="gw_layout"
          workspaceName="layout-fixture"
          sessionId="ses_layout"
          activeFilePath={null}
          searchOpen={searchOpen}
          collapseVersion={0}
          expandedPaths={[WORKSPACE_ROOT]}
          onExpandedPathsChange={ignoreExpandedPathsChange}
          onCloseSearch={ignoreSearchClose}
          onOpenFile={ignoreFileOpen}
          onStatusChange={setFileTreeStatus}
        />
      </section>
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
