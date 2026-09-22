import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { SessionFileChange } from "../../types/backend";
import WorkspaceFilePreviewArea, {
  type WorkspacePreviewTab,
} from "./WorkspaceFilePreviewArea";

const MAX_RENDERED_DIFF_LINES = 2000;

function diffTab(lineCount: number): WorkspacePreviewTab {
  const diffText = Array.from(
    { length: lineCount },
    (_, index) => (index % 2 === 0 ? `+line ${index}` : `-line ${index}`),
  ).join("\n");
  const change: SessionFileChange = {
    file_path: "src/big.ts",
    kind: "edit",
    additions: 1,
    deletions: 1,
    reviewed: false,
    latest_edit_id: "edit_1",
    tool_call_ids: [],
    execution_ids: [],
    turn_ids: [],
    diff_file: "diff_1",
    diff_text: diffText,
  };
  return {
    previewType: "session-diff",
    path: "src/big.ts",
    name: "big.ts",
    change,
    changesetLabel: "默认变更集",
  };
}

function renderPreview(tab: WorkspacePreviewTab): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WorkspaceFilePreviewArea
        context="changes"
        visible
        flexRatio={1}
        apiPort={8014}
        workspaceId="ws_test"
        workspaceName="project"
        sessionTitle="session"
        tabs={[tab]}
        activePath={tab.path}
        loadingPath={null}
        error={null}
        editingPath={null}
        draftContent=""
        savingPath={null}
        hasUnsavedEdit={false}
        markdownSourceVisible={false}
        onMarkdownSourceChange={() => {}}
        onBeginEdit={() => {}}
        onDraftChange={() => {}}
        onCancelEdit={() => {}}
        onSaveEdit={async () => {}}
        onOpenWorkspacePath={async () => {}}
      />,
    );
  });
  return renderer;
}

function diffLineCount(renderer: ReactTestRenderer): number {
  return renderer.root.findAll(
    (node) =>
      typeof node.props.className === "string"
      && node.props.className.includes("workspace-preview-diff-line"),
  ).length;
}

describe("超大 diff 的渲染上限", () => {
  test("超过阈值时只渲染上限行数并给出含总行数的提示", () => {
    const tab = diffTab(MAX_RENDERED_DIFF_LINES + 250);
    const renderer = renderPreview(tab);

    expect(diffLineCount(renderer)).toBe(MAX_RENDERED_DIFF_LINES);
    const notice = renderer.root.findByProps({
      className: "workspace-preview-truncation-notice",
    });
    expect(notice.props.role).toBe("status");
    const text = JSON.stringify(renderer.toJSON());
    expect(text).toContain(String(MAX_RENDERED_DIFF_LINES + 250));
    expect(text).toContain(String(MAX_RENDERED_DIFF_LINES));
    renderer.unmount();
  });

  test("提示提供查看完整内容的入口，点击后渲染全部行", () => {
    const total = MAX_RENDERED_DIFF_LINES + 250;
    const renderer = renderPreview(diffTab(total));

    const expandButton = renderer.root.find(
      (node) =>
        node.type === "button"
        && JSON.stringify(node.props.children ?? "").includes(`全部 ${total} 行`),
    );
    act(() => expandButton.props.onClick());

    expect(diffLineCount(renderer)).toBe(total);
    renderer.unmount();
  });

  test("未超过阈值时不展示提示且渲染全部行", () => {
    const renderer = renderPreview(diffTab(120));

    expect(diffLineCount(renderer)).toBe(120);
    expect(
      renderer.root.findAllByProps({
        className: "workspace-preview-truncation-notice",
      }).length,
    ).toBe(0);
    renderer.unmount();
  });
});
