import {
  isLikelyWorkspaceFileReference,
  parseWorkspaceFileReference,
  plainWorkspaceFileReferences,
  remarkWorkspaceFileReferences,
} from "../../utils/workspaceFileReferences";

const workspaceRoot = "/home/user/project";

const relative = parseWorkspaceFileReference("src/main.py#L12-L14", workspaceRoot);
if (
  relative?.path !== "src/main.py" ||
  relative.selection?.startLine !== 12 ||
  relative.selection.endLine !== 14
) {
  throw new Error("相对文件路径或行范围解析失败");
}

const absolute = parseWorkspaceFileReference(
  "/home/user/project/src/app.ts:8:3",
  workspaceRoot,
);
if (
  absolute?.path !== "src/app.ts" ||
  absolute.selection?.startLine !== 8 ||
  absolute.selection.startColumn !== 3
) {
  throw new Error("工作区绝对路径或行列解析失败");
}

if (parseWorkspaceFileReference("/etc/passwd", workspaceRoot) !== null) {
  throw new Error("工作区外绝对路径不应成为文件引用");
}
if (parseWorkspaceFileReference("https://example.com/app.ts", workspaceRoot) !== null) {
  throw new Error("外部 URL 不应成为工作区文件引用");
}
if (parseWorkspaceFileReference("../secret.txt", workspaceRoot) !== null) {
  throw new Error("越界相对路径不应成为文件引用");
}

for (const target of ["running", "url", "ses_e878f6e0aade", "openBrowserPage", "包含 空格"]) {
  if (isLikelyWorkspaceFileReference(target)) {
    throw new Error(`普通 inline code 不应触发文件探测: ${target}`);
  }
}
for (const target of ["src/main.py", "AGENTS.md", "./README.md#L3"]) {
  if (!isLikelyWorkspaceFileReference(target)) {
    throw new Error(`文件形态的 inline code 应触发文件探测: ${target}`);
  }
}

const plain = plainWorkspaceFileReferences(
  "查看 src/main.py、AGENTS.md 和 https://example.com/docs.html",
);
if (plain.map((item) => item.target).join("|") !== "src/main.py|AGENTS.md") {
  throw new Error(`普通文本文件引用识别错误: ${JSON.stringify(plain)}`);
}

const tree = {
  type: "root",
  children: [
    { type: "paragraph", children: [{ type: "text", value: "打开 src/main.py 查看" }] },
    { type: "inlineCode", value: "src/keep.ts" },
    {
      type: "link",
      url: "https://example.com",
      children: [{ type: "text", value: "docs/example.md" }],
    },
  ],
};
remarkWorkspaceFileReferences()(tree);
const linkedParagraph = tree.children[0];
const linkedChild = linkedParagraph.children?.[1] as
  | { type: string; url?: string }
  | undefined;
if (
  !linkedParagraph.children ||
  linkedChild?.type !== "link" ||
  !linkedChild.url?.includes("src%2Fmain.py")
) {
  throw new Error("Markdown 普通文本没有转换成文件引用链接");
}
if (tree.children[1]?.type !== "inlineCode") {
  throw new Error("remark 插件不应改写 inlineCode 节点");
}
if (tree.children[2]?.children?.[0]?.type !== "text") {
  throw new Error("remark 插件不应改写已有外部链接的文本");
}

console.log("workspaceFileReferences tests passed");
