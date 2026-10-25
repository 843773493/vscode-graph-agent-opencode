import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import * as api from "../../api";
import NodeDebugSourcePreview from "./NodeDebugSourcePreview";

function props() {
  return {
    apiPort: 8010,
    workspaceId: "workspace-test",
    path: "src/main.ts",
    focusLine: 1,
    sourceRevision: 0,
    breakpoints: [],
    disabled: false,
    onChangeBreakpoint: () => undefined,
    onOpenWorkspacePath: async () => undefined,
  };
}

describe("NodeDebugSourcePreview", () => {
  let renderer: ReactTestRenderer | null = null;
  let restoreContentRequest: (() => void) | null = null;

  afterEach(async () => {
    if (renderer) {
      await act(async () => renderer?.unmount());
      renderer = null;
    }
    restoreContentRequest?.();
    restoreContentRequest = null;
  });

  test("读取失败仍保留源码预览外框和固定视口", async () => {
    let rejectRequest!: (cause: unknown) => void;
    const contentRequest = new Promise<never>((_resolve, reject) => {
      rejectRequest = reject;
    });
    const getContent = spyOn(api, "getWorkspaceFileContent").mockReturnValue(contentRequest);
    restoreContentRequest = () => getContent.mockRestore();

    await act(async () => {
      renderer = create(<NodeDebugSourcePreview {...props()} />);
    });

    const preview = renderer!.root.findByProps({ className: "node-debug-source-preview" });
    const viewport = preview.findByProps({ className: "node-debug-code" });
    expect(viewport.props.role).toBe("status");
    expect(preview.findAllByType("header")).toHaveLength(1);
    expect(preview.findAllByType("small")).toHaveLength(1);

    await act(async () => {
      rejectRequest(new Error("源码读取失败"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const errorViewport = renderer!.root.findByProps({ className: "node-debug-code" });
    expect(errorViewport.props.role).toBe("alert");
    expect(errorViewport.findByProps({ className: "node-debug-source-state error" }).children).toEqual([
      "源码读取失败",
    ]);
    expect(renderer!.root.findAllByType("header")).toHaveLength(1);
    expect(renderer!.root.findAllByType("small")).toHaveLength(1);
    expect(getContent).toHaveBeenCalledWith(8010, "src/main.ts", "workspace-test");
  });
});
