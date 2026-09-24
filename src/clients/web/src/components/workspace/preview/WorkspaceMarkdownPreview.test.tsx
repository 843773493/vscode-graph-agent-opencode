import { describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import WorkspaceMarkdownPreview, {
  LARGE_MARKDOWN_PREVIEW_LENGTH,
} from "./WorkspaceMarkdownPreview";

/**
 * 大文档 Markdown 预览的渲染上限。
 *
 * 真实浏览器审查记录：后端 workspace.files.preview.max_bytes 默认 1 MiB，把
 * GFM 宽表塞进这个额度内（实测 291 KB 表格）会让 remark-gfm 在主线程同步解析
 * 118.8 秒，1 MiB 更超过 10 分钟仍未返回，期间界面完全假死。预览必须对超限文档
 * 做上限渲染并给出显式提示；「源码」视图仍能查看完整正文，不是静默截断。
 *
 * 本用例杀掉「删掉 slice 上限」「删掉提示」两类变异。
 */

const OVER_THRESHOLD_LENGTH = LARGE_MARKDOWN_PREVIEW_LENGTH + 5_000;
/** 位于上限之后、只有渲染完整文档时才会出现的唯一标记。 */
const BEYOND_LIMIT_MARKER = "截断之后才出现的唯一标记";
const WITHIN_LIMIT_MARKER = "上限之内的唯一标记";

function overThresholdContent(): string {
  return (
    WITHIN_LIMIT_MARKER
    + "x".repeat(OVER_THRESHOLD_LENGTH)
    + BEYOND_LIMIT_MARKER
  );
}

function render(props: { content: string }): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <WorkspaceMarkdownPreview
        apiPort={8014}
        workspaceId="ws_test"
        path="docs/guide.md"
        content={props.content}
        onOpenWorkspacePath={async () => undefined}
      />,
    );
  });
  return renderer;
}

function staticMarkup(content: string): string {
  return renderToStaticMarkup(
    <WorkspaceMarkdownPreview
      apiPort={8014}
      workspaceId="ws_test"
      path="docs/guide.md"
      content={content}
      onOpenWorkspacePath={async () => undefined}
    />,
  );
}

describe("大文档 Markdown 预览的渲染上限", () => {
  test("超过上限时只解析前 N 个字符，上限之后的正文不进入渲染", () => {
    const markup = staticMarkup(overThresholdContent());

    expect(markup).toContain(WITHIN_LIMIT_MARKER);
    expect(markup).not.toContain(BEYOND_LIMIT_MARKER);
  });

  test("超过上限时展示含总字符数的提示，并给出渲染完整文档的入口", () => {
    const content = overThresholdContent();
    const markup = staticMarkup(content);

    expect(markup).toContain("workspace-preview-truncation-notice");
    expect(markup).toContain(content.length.toLocaleString());
    expect(markup).toContain("渲染完整 Markdown");
  });

  test("点击入口后在原位渲染完整文档", () => {
    const renderer = render({ content: overThresholdContent() });

    const expandButton = renderer.root.find(
      (node) =>
        node.type === "button"
        && JSON.stringify(node.props.children ?? "").includes("渲染完整 Markdown"),
    );
    expect(JSON.stringify(renderer.toJSON())).not.toContain(BEYOND_LIMIT_MARKER);

    act(() => expandButton.props.onClick());

    const expanded = JSON.stringify(renderer.toJSON());
    expect(expanded).toContain(BEYOND_LIMIT_MARKER);
    expect(expanded).toContain(
      "仅显示前 " + LARGE_MARKDOWN_PREVIEW_LENGTH.toLocaleString(),
    );
    act(() => renderer.unmount());
  });

  test("未超过上限时不展示提示且渲染完整正文", () => {
    const content = WITHIN_LIMIT_MARKER + "x".repeat(LARGE_MARKDOWN_PREVIEW_LENGTH - 20);
    const renderer = render({ content });

    expect(
      renderer.root.findAllByProps({
        className: "workspace-preview-truncation-notice",
      }).length,
    ).toBe(0);
    expect(JSON.stringify(renderer.toJSON())).toContain(WITHIN_LIMIT_MARKER);
    act(() => renderer.unmount());
  });
});
