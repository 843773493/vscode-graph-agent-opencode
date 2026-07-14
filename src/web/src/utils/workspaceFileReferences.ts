export interface WorkspaceFileSelection {
  startLine: number;
  endLine: number;
  startColumn?: number;
}

export interface WorkspaceFileReference {
  path: string;
  selection: WorkspaceFileSelection | null;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

const GENERATED_REFERENCE_PREFIX = "/__boxteam_workspace_file__?target=";
const PLAIN_FILE_PATH_PATTERN = /[^\s`*${}()<>,;!?、，。；：！？]+\.[^\s`*${}()<>,;!?、，。；：！？]+/gu;
const TRAILING_PUNCTUATION_PATTERN = /[,.!?;，。！？；]+$/u;

export function isLikelyWorkspaceFileReference(target: string): boolean {
  const normalized = target.trim().replace(/\\/g, "/");
  if (!normalized || /\s/u.test(normalized)) {
    return false;
  }
  if (
    normalized.startsWith(GENERATED_REFERENCE_PREFIX) ||
    normalized.startsWith("./") ||
    normalized.startsWith("../") ||
    normalized.includes("/")
  ) {
    return true;
  }
  return /^[^./]+\.[a-z\d][a-z\d._-]*?(?::\d+(?::\d+)?)?(?:#L?\d+(?:-L?\d+)?)?$/iu.test(
    normalized,
  );
}

function decodedTarget(value: string): string | null {
  try {
    return decodeURI(value);
  } catch {
    return null;
  }
}

function parseSelection(value: string): {
  target: string;
  selection: WorkspaceFileSelection | null;
} {
  const hashMatch = /#L?(\d+)(?:-L?(\d+))?$/.exec(value);
  if (hashMatch) {
    const startLine = Number(hashMatch[1]);
    const endLine = Number(hashMatch[2] ?? hashMatch[1]);
    return {
      target: value.slice(0, hashMatch.index),
      selection: { startLine, endLine },
    };
  }

  const suffixMatch = /:(\d+)(?::(\d+))?$/.exec(value);
  if (!suffixMatch) {
    return { target: value, selection: null };
  }
  return {
    target: value.slice(0, suffixMatch.index),
    selection: {
      startLine: Number(suffixMatch[1]),
      endLine: Number(suffixMatch[1]),
      startColumn: suffixMatch[2] ? Number(suffixMatch[2]) : undefined,
    },
  };
}

function normalizeRelativePath(target: string, workspaceRoot: string): string | null {
  const normalizedRoot = workspaceRoot.replace(/\\/g, "/").replace(/\/$/, "");
  let normalizedTarget = target.replace(/\\/g, "/");

  if (normalizedTarget.startsWith("file://")) {
    normalizedTarget = normalizedTarget.slice("file://".length);
  } else if (/^[a-z][a-z\d+.-]*:/i.test(normalizedTarget)) {
    return null;
  }

  if (normalizedTarget.startsWith("/")) {
    if (!normalizedRoot || normalizedTarget === normalizedRoot) {
      return null;
    }
    const workspacePrefix = `${normalizedRoot}/`;
    if (!normalizedTarget.startsWith(workspacePrefix)) {
      return null;
    }
    normalizedTarget = normalizedTarget.slice(workspacePrefix.length);
  }

  normalizedTarget = normalizedTarget.replace(/^\.\//, "");
  const segments: string[] = normalizedTarget
    .split("/")
    .filter((segment: string) => Boolean(segment) && segment !== ".");
  if (segments.length === 0 || segments.some((segment) => segment === "..")) {
    return null;
  }
  return segments.join("/");
}

export function parseWorkspaceFileReference(
  rawTarget: string,
  workspaceRoot: string,
): WorkspaceFileReference | null {
  let target = rawTarget.trim();
  if (target.startsWith(GENERATED_REFERENCE_PREFIX)) {
    target = new URLSearchParams(target.slice(target.indexOf("?") + 1)).get("target") ?? "";
  }
  if (!target || target.startsWith("#")) {
    return null;
  }

  const decoded = decodedTarget(target);
  if (!decoded) {
    return null;
  }
  const { target: pathTarget, selection } = parseSelection(decoded);
  const path = normalizeRelativePath(pathTarget, workspaceRoot);
  return path ? { path, selection } : null;
}

export function workspaceFileReferenceHref(target: string): string {
  return `${GENERATED_REFERENCE_PREFIX}${encodeURIComponent(target)}`;
}

export function plainWorkspaceFileReferences(value: string): Array<{
  start: number;
  end: number;
  target: string;
}> {
  const references: Array<{ start: number; end: number; target: string }> = [];
  for (const match of value.matchAll(PLAIN_FILE_PATH_PATTERN)) {
    if (match.index === undefined || match[0].includes("://")) {
      continue;
    }
    const target = match[0].replace(TRAILING_PUNCTUATION_PATTERN, "");
    if (!target || target.startsWith("@") || target.includes("@")) {
      continue;
    }
    references.push({
      start: match.index,
      end: match.index + target.length,
      target,
    });
  }
  return references;
}

function linkifyTextNode(node: MarkdownNode): MarkdownNode[] {
  const value = node.value ?? "";
  const references = plainWorkspaceFileReferences(value);
  if (references.length === 0) {
    return [node];
  }

  const result: MarkdownNode[] = [];
  let offset = 0;
  for (const reference of references) {
    if (reference.start > offset) {
      result.push({ type: "text", value: value.slice(offset, reference.start) });
    }
    result.push({
      type: "link",
      url: workspaceFileReferenceHref(reference.target),
      children: [{ type: "text", value: reference.target }],
    });
    offset = reference.end;
  }
  if (offset < value.length) {
    result.push({ type: "text", value: value.slice(offset) });
  }
  return result;
}

function transformMarkdownChildren(node: MarkdownNode): void {
  if (!node.children || ["code", "inlineCode", "link"].includes(node.type)) {
    return;
  }
  node.children = node.children.flatMap((child) => {
    if (child.type === "text") {
      return linkifyTextNode(child);
    }
    transformMarkdownChildren(child);
    return [child];
  });
}

export function remarkWorkspaceFileReferences() {
  return (tree: MarkdownNode) => {
    transformMarkdownChildren(tree);
  };
}
