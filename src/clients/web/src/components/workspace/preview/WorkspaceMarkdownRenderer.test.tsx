import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import WorkspaceMarkdownRenderer from "./WorkspaceMarkdownRenderer";

const path = "boxteam://workspace/ws/resources/files/root/docs/start.md";

test("标题沿 GitHub slug 规则，重复标题及跨文件锚点保留定位", async () => {
  const opens: string[][] = [];
  const scrolls: string[] = [];
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(<WorkspaceMarkdownRenderer apiPort={0} workspaceId="ws" path={path}
      content={'# 安装 *说明*\n\n# 安装 *说明*\n\n[页内](#%E5%AE%89%E8%A3%85-%E8%AF%B4%E6%98%8E-1)\n\n[下一页](next.md#%E5%AE%89%E8%A3%85)'}
      onOpenWorkspacePath={async (path, fragment) => { opens.push([path, fragment!]); }} />,
      { createNodeMock: node => node.props.className === "workspace-markdown-document" ? {
        querySelectorAll: () => [{ id: "安装-说明-1", scrollIntoView: () => scrolls.push("duplicate") }],
      } : null });
  });
  expect(renderer.root.findAllByType("h1").map(node => node.props.id)).toEqual(["安装-说明", "安装-说明-1"]);
  const links = renderer.root.findAllByType("a");
  const event = { preventDefault() {} };
  act(() => links[0].props.onClick(event));
  expect(scrolls).toEqual(["duplicate"]);
  await act(async () => links[1].props.onClick(event));
  expect(opens).toEqual([["boxteam://workspace/ws/resources/files/root/docs/next.md", "#%E5%AE%89%E8%A3%85"]]);
  act(() => renderer.unmount());
});

test("跨文件打开及展开正文后重新定位，不改变页面 hash", () => {
  const scrolls: string[] = [];
  let full = false;
  const view = (content: string) => <WorkspaceMarkdownRenderer apiPort={0} workspaceId="ws" path={path}
    content={content} fragment="#late" onOpenWorkspacePath={async () => {}} />;
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(view("# First"), { createNodeMock: node => node.props.className === "workspace-markdown-document" ? {
    querySelectorAll: () => full ? [{ id: "late", scrollIntoView: () => scrolls.push("late") }] : [],
  } : null }); });
  expect(renderer.root.findByProps({ role: "alert" }).children.join("")).toContain("#late");
  full = true;
  act(() => renderer.update(view("# First\n\n# Late")));
  expect(scrolls).toEqual(["late"]);
  expect(renderer.root.findAllByProps({ role: "alert" })).toHaveLength(0);
  act(() => renderer.unmount());
});

test("规范资源 URI 保留锚点，危险协议仍由标准清理链拦截", async () => {
  const opens: string[][] = [];
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<WorkspaceMarkdownRenderer apiPort={0} workspaceId="ws" path={path}
    content={`[规范引用](boxteam://workspace/ws/resources/files/root/next.md#heading)\n\n[危险](javascript:alert)\n\n[外部](https://example.com/)`}
    onOpenWorkspacePath={async (path, fragment) => { opens.push([path, fragment!]); }} />); });
  const links = renderer.root.findAllByType("a");
  expect(links.map(link => link.props.href)).toEqual(["boxteam://workspace/ws/resources/files/root/next.md#heading", "https://example.com/"]);
  await act(async () => links[0].props.onClick({ preventDefault() {} }));
  expect(opens).toEqual([["boxteam://workspace/ws/resources/files/root/next.md", "#heading"]]);
  expect(links[1].props.rel).toBe("noopener noreferrer");
  act(() => renderer.unmount());
});
