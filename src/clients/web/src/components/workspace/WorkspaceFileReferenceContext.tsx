import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import {
  HttpRequestError,
  getWorkspaceFileContent,
  getWorkspaceFiles,
} from "../../api";
import type { WorkspaceFileContent, WorkspaceFileNode } from "../../types/backend";
import {
  isExplicitWorkspaceFileTarget,
  isWorkspaceTextFilePath,
  parseWorkspaceFileReference,
  type WorkspaceFileReference,
} from "../../utils/workspaceFileReferences";
import { errorDisplayMessage } from "../../utils/errorMessage";

export type WorkspaceFileReferenceResolution =
  | {
      status: "resolved";
      reference: WorkspaceFileReference;
      content: WorkspaceFileContent;
    }
  | { status: "missing" }
  | { status: "error"; message: string };

interface WorkspaceFileReferenceContextValue {
  resolve: (target: string) => Promise<WorkspaceFileReferenceResolution>;
  open: (
    resolution: Extract<WorkspaceFileReferenceResolution, { status: "resolved" }>,
  ) => void;
}

type WorkspaceFileLookup =
  | { status: "resolved"; content: WorkspaceFileContent }
  | { status: "missing" }
  | { status: "error"; message: string };

type WorkspaceFilePathLookup =
  | { status: "found"; path: string }
  | { status: "missing" }
  | { status: "ambiguous" };

const MAX_FILE_TREE_DIRECTORIES = 256;

function isProtectedWorkspaceMetadataPath(path: string): boolean {
  const normalizedPath = path.replace(/\\/g, "/").replace(/^\.\//, "");
  return normalizedPath === ".boxteam" || normalizedPath.startsWith(".boxteam/");
}

async function findUniqueWorkspaceFilePath(
  apiPort: number,
  workspaceId: string | null,
  referencePath: string,
): Promise<WorkspaceFilePathLookup> {
  const normalizedReferencePath = referencePath.replace(/^\.\//, "");
  const pathSegments = normalizedReferencePath.split("/").filter(Boolean);
  const fileName = pathSegments[pathSegments.length - 1] ?? "";
  const directories = [{ path: "", depth: 0, relativePath: "" }];
  const matches: string[] = [];
  let visitedDirectories = 0;

  while (directories.length > 0) {
    const directory = directories.shift();
    if (!directory) break;
    visitedDirectories += 1;
    if (visitedDirectories > MAX_FILE_TREE_DIRECTORIES) {
      return { status: "ambiguous" };
    }

    let cursor: string | null = null;
    do {
      let listing;
      try {
        listing = await getWorkspaceFiles(
          apiPort,
          directory.path,
          workspaceId,
          undefined,
          cursor,
        );
      } catch (error: unknown) {
        // 会话运行时目录可能在递归查找期间被创建、压缩或回收；已经消失的
        // 目录不是工作区文件引用错误。其它 HTTP/网络错误仍交给调用方展示。
        if (error instanceof HttpRequestError && error.status === 404) {
          break;
        }
        throw error;
      }
      for (const node of listing.items as WorkspaceFileNode[]) {
        const relativePath = [directory.relativePath, node.name].filter(Boolean).join("/");
        if (isProtectedWorkspaceMetadataPath(relativePath)) {
          continue;
        }
        if (node.kind === "directory") {
          if (directory.depth < 12) {
            directories.push({ path: node.path, depth: directory.depth + 1, relativePath });
          }
        } else if (
          node.name === fileName
          && (
            !normalizedReferencePath.includes("/")
            || relativePath === normalizedReferencePath
            || relativePath.endsWith(`/${normalizedReferencePath}`)
          )
        ) {
          matches.push(node.path);
          if (matches.length > 1) return { status: "ambiguous" };
        }
      }
      cursor = listing.next_cursor ?? null;
    } while (cursor);
  }

  return matches[0]
    ? { status: "found", path: matches[0] }
    : { status: "missing" };
}

const WorkspaceFileReferenceContext = createContext<WorkspaceFileReferenceContextValue | null>(
  null,
);

export function WorkspaceFileReferenceProvider({
  apiPort,
  workspaceId,
  onOpen,
  children,
}: {
  apiPort: number;
  workspaceId: string | null;
  onOpen: (
    content: WorkspaceFileContent,
    reference: WorkspaceFileReference,
  ) => void;
  children: React.ReactNode;
}) {
  const cacheRef = useRef(
    new Map<string, Promise<WorkspaceFileLookup>>(),
  );

  useEffect(() => {
    cacheRef.current.clear();
  }, [apiPort, workspaceId]);

  const resolve = useCallback(
    async (target: string): Promise<WorkspaceFileReferenceResolution> => {
      if (!workspaceId) return { status: "error", message: "文件引用缺少工作区身份" };
      let reference: WorkspaceFileReference | null;
      try {
        reference = parseWorkspaceFileReference(target);
      } catch (error: unknown) {
        return { status: "error", message: errorDisplayMessage(error) };
      }
      if (!reference) {
        return { status: "missing" };
      }
      if (!isWorkspaceTextFilePath(reference.path)) {
        return { status: "missing" };
      }
      const cacheKey = `${workspaceId}:${reference.path}`;
      let request = cacheRef.current.get(cacheKey);
      if (!request) {
        request = (async (): Promise<WorkspaceFileLookup> => {
          try {
            try {
              return { status: "resolved", content: await getWorkspaceFileContent(apiPort, reference.path, workspaceId) };
            } catch (error: unknown) {
              if (!(error instanceof HttpRequestError && error.status === 404)
                  || isExplicitWorkspaceFileTarget(reference.path) || reference.path.includes("\\")
                  || reference.path.split("/").includes("..")) throw error;
            }
            // 仅不存在的相对简写保留唯一文件名查找；显式目标失败绝不改投同名文件。
            const lookup = await findUniqueWorkspaceFilePath(apiPort, workspaceId, reference.path);
            if (lookup.status === "missing") return { status: "missing" };
            if (lookup.status === "ambiguous") {
              return { status: "error", message: `文件名不唯一，无法确定工作区中的 ${reference.path}` };
            }
            return { status: "resolved", content: await getWorkspaceFileContent(apiPort, lookup.path, workspaceId) };
          } catch (error: unknown) {
            return error instanceof HttpRequestError && error.status === 404
              ? { status: "missing" }
              : { status: "error", message: errorDisplayMessage(error) };
          }
        })();
        cacheRef.current.set(cacheKey, request);
      }
      return request.then((lookup): WorkspaceFileReferenceResolution =>
        lookup.status === "resolved"
          ? { ...lookup, reference: { ...reference, path: lookup.content.path } }
          : lookup,
      );
    },
    [apiPort, workspaceId],
  );

  const open = useCallback(
    (
      resolution: Extract<
        WorkspaceFileReferenceResolution,
        { status: "resolved" }
      >,
    ) => {
      onOpen(resolution.content, resolution.reference);
    },
    [onOpen],
  );

  const value = useMemo(() => ({ resolve, open }), [open, resolve]);
  return (
    <WorkspaceFileReferenceContext.Provider value={value}>
      {children}
    </WorkspaceFileReferenceContext.Provider>
  );
}

export function useWorkspaceFileReferenceContext() {
  return useContext(WorkspaceFileReferenceContext);
}
