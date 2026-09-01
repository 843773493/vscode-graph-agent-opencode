import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import {
  DEFAULT_BACKEND_PORT,
  getWorkspaceFileContent,
  getWorkspaceFiles,
} from "../../api";
import type { WorkspaceFileContent, WorkspaceFileNode } from "../../types/backend";
import {
  isWorkspaceTextFilePath,
  parseWorkspaceFileReference,
  type WorkspaceFileReference,
} from "../../utils/workspaceFileReferences";

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

async function findUniqueWorkspaceFilePath(
  apiPort: number,
  workspaceId: string | null,
  referencePath: string,
): Promise<WorkspaceFilePathLookup> {
  const normalizedReferencePath = referencePath.replace(/\\/g, "/").replace(/^\.\//, "");
  const fileName = normalizedReferencePath.split("/").filter(Boolean).at(-1) ?? "";
  const directories: Array<{ path: string; depth: number }> = [{ path: "", depth: 0 }];
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
      const listing = await getWorkspaceFiles(
        apiPort,
        directory.path,
        workspaceId,
        undefined,
        cursor,
      );
      for (const node of (listing.items ?? []) as WorkspaceFileNode[]) {
        if (node.kind === "directory") {
          if (directory.depth < 12) {
            directories.push({ path: node.path, depth: directory.depth + 1 });
          }
        } else if (
          node.name === fileName
          && (
            !normalizedReferencePath.includes("/")
            || node.path === normalizedReferencePath
            || node.path.endsWith(`/${normalizedReferencePath}`)
          )
        ) {
          matches.push(node.path.replace(/\\/g, "/"));
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
  workspaceRoot,
  onOpen,
  children,
}: {
  apiPort: number;
  workspaceId: string | null;
  workspaceRoot: string;
  onOpen: (
    content: WorkspaceFileContent,
    reference: WorkspaceFileReference,
  ) => void;
  children: React.ReactNode;
}) {
  const cacheRef = useRef(
    new Map<string, Promise<WorkspaceFileLookup>>(),
  );
  const pathCacheRef = useRef(
    new Map<string, Promise<WorkspaceFilePathLookup>>(),
  );

  useEffect(() => {
    cacheRef.current.clear();
    pathCacheRef.current.clear();
  }, [apiPort, workspaceId, workspaceRoot]);

  const resolve = useCallback(
    async (target: string): Promise<WorkspaceFileReferenceResolution> => {
      const reference = parseWorkspaceFileReference(target, workspaceRoot);
      if (!reference) {
        return { status: "missing" };
      }
      if (!isWorkspaceTextFilePath(reference.path)) {
        return { status: "missing" };
      }
      let resolvedPath = reference.path;
      const pathKey = `${workspaceId ?? "local"}:${reference.path}`;
      let pathRequest = pathCacheRef.current.get(pathKey);
      if (!pathRequest) {
        pathRequest = findUniqueWorkspaceFilePath(
          apiPort ?? DEFAULT_BACKEND_PORT,
          workspaceId,
          reference.path,
        );
        pathCacheRef.current.set(pathKey, pathRequest);
      }
      const pathLookup = await pathRequest;
      if (pathLookup.status === "missing") return { status: "missing" };
      if (pathLookup.status === "ambiguous") {
        return {
          status: "error",
          message: `文件名不唯一，无法确定工作区中的 ${reference.path}`,
        };
      }
      resolvedPath = pathLookup.path;

      const resolvedReference = { ...reference, path: resolvedPath };
      const cacheKey = `${workspaceId ?? "local"}:${resolvedPath}`;
      let request = cacheRef.current.get(cacheKey);
      if (!request) {
        request = getWorkspaceFileContent(
          apiPort ?? DEFAULT_BACKEND_PORT,
          resolvedPath,
          workspaceId,
        )
          .then(
            (content): WorkspaceFileLookup => ({
              status: "resolved",
              content,
            }),
          )
          .catch((error: unknown): WorkspaceFileLookup => {
            const message = error instanceof Error ? error.message : String(error);
            if (message.includes("请求失败 404")) {
              return { status: "missing" };
            }
            console.error(`文件引用验证失败: target=${target}`, error);
            return { status: "error", message };
          });
        cacheRef.current.set(cacheKey, request);
      }
      return request.then((lookup): WorkspaceFileReferenceResolution =>
        lookup.status === "resolved"
          ? { ...lookup, reference: resolvedReference }
          : lookup,
      );
    },
    [apiPort, workspaceId, workspaceRoot],
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
