import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { DEFAULT_BACKEND_PORT, getWorkspaceFileContent } from "../../api";
import type { WorkspaceFileContent } from "../../types/backend";
import {
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

  useEffect(() => {
    cacheRef.current.clear();
  }, [apiPort, workspaceId, workspaceRoot]);

  const resolve = useCallback(
    (target: string): Promise<WorkspaceFileReferenceResolution> => {
      const reference = parseWorkspaceFileReference(target, workspaceRoot);
      if (!reference) {
        return Promise.resolve({ status: "missing" });
      }
      const cacheKey = `${workspaceId ?? "local"}:${reference.path}`;
      let request = cacheRef.current.get(cacheKey);
      if (!request) {
        request = getWorkspaceFileContent(
          apiPort ?? DEFAULT_BACKEND_PORT,
          reference.path,
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
          ? { ...lookup, reference }
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
