import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEFAULT_BACKEND_PORT, getWorkspaceFiles } from "../../api";
import type { WorkspaceFileNode } from "../../types/backend";

interface DirectoryState {
  items: WorkspaceFileNode[];
  loading: boolean;
  error: string | null;
  truncated: boolean;
}

interface WorkspaceFileTreeProps {
  apiPort: number | null;
  workspaceId: string | null;
  workspaceName: string | null;
  workspaceRoot: string | null;
  activeFilePath: string | null;
  searchOpen: boolean;
  collapseVersion: number;
  onOpenFile: (node: WorkspaceFileNode) => void;
  onStatusChange: (text: string) => void;
}

const ROOT_PATH = "";

function fileIcon(node: WorkspaceFileNode): string {
  if (node.kind === "directory") {
    return "▣";
  }
  if (node.kind === "symlink") {
    return "↪";
  }
  return "◇";
}

function formatFileSize(size: number | null | undefined): string {
  if (typeof size !== "number") {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function shortWorkspaceLabel(workspaceRoot: string | null, workspaceName: string | null): string {
  return workspaceName || workspaceRoot?.split(/[\\/]/).filter(Boolean).pop() || "workspace";
}

export default function WorkspaceFileTree({
  apiPort,
  workspaceId,
  workspaceName,
  workspaceRoot,
  activeFilePath,
  searchOpen,
  collapseVersion,
  onOpenFile,
  onStatusChange,
}: WorkspaceFileTreeProps) {
  const port = apiPort ?? DEFAULT_BACKEND_PORT;
  const rootLabel = useMemo(
    () => shortWorkspaceLabel(workspaceRoot, workspaceName),
    [workspaceName, workspaceRoot],
  );
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(
    () => new Set([ROOT_PATH]),
  );
  const [directories, setDirectories] = useState<Record<string, DirectoryState>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const lastCollapseVersionRef = useRef(collapseVersion);

  const loadDirectory = useCallback(
    async (path: string): Promise<boolean> => {
      setDirectories((prev) => ({
        ...prev,
        [path]: {
          items: prev[path]?.items ?? [],
          loading: true,
          error: null,
          truncated: prev[path]?.truncated ?? false,
        },
      }));

      try {
        const result = await getWorkspaceFiles(port, path, workspaceId);
        setDirectories((prev) => ({
          ...prev,
          [path]: {
            items: result.items ?? [],
            loading: false,
            error: null,
            truncated: result.truncated ?? false,
          },
        }));
        return true;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setDirectories((prev) => ({
          ...prev,
          [path]: {
            items: prev[path]?.items ?? [],
            loading: false,
            error: message,
            truncated: prev[path]?.truncated ?? false,
          },
        }));
        onStatusChange(`文件树加载失败: ${message}`);
        return false;
      }
    },
    [onStatusChange, port, workspaceId],
  );

  useEffect(() => {
    setExpandedPaths(new Set([ROOT_PATH]));
    setDirectories({});
    void loadDirectory(ROOT_PATH);
  }, [loadDirectory, workspaceId, workspaceRoot]);

  useEffect(() => {
    if (lastCollapseVersionRef.current === collapseVersion) {
      return;
    }
    lastCollapseVersionRef.current = collapseVersion;
    setExpandedPaths(new Set([ROOT_PATH]));
    onStatusChange("文件树已全部折叠");
  }, [collapseVersion, onStatusChange]);

  useEffect(() => {
    if (!searchOpen) {
      setSearchQuery("");
    }
  }, [searchOpen]);

  const handleRootClick = () => {
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(ROOT_PATH)) {
        next.delete(ROOT_PATH);
      } else {
        next.add(ROOT_PATH);
        if (!directories[ROOT_PATH]) {
          void loadDirectory(ROOT_PATH);
        }
      }
      return next;
    });
    onStatusChange(`工作区根目录: ${workspaceRoot || rootLabel}`);
  };

  const handleNodeClick = (node: WorkspaceFileNode) => {
    if (node.kind !== "directory") {
      const size = formatFileSize(node.size);
      onOpenFile(node);
      onStatusChange(size ? `${node.path} · ${size}` : node.path);
      return;
    }

    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(node.path)) {
        next.delete(node.path);
      } else {
        next.add(node.path);
        if (!directories[node.path]) {
          void loadDirectory(node.path);
        }
      }
      return next;
    });
  };

  const nodeMatchesSearch = (node: WorkspaceFileNode): boolean => {
    const normalizedQuery = searchQuery.trim().toLowerCase();
    if (!normalizedQuery) {
      return true;
    }
    const nodeTextMatches = `${node.name}\n${node.path}`.toLowerCase().includes(normalizedQuery);
    if (nodeTextMatches || node.kind !== "directory") {
      return nodeTextMatches;
    }
    return (directories[node.path]?.items ?? []).some(nodeMatchesSearch);
  };

  const renderDirectory = (path: string, depth: number) => {
    const directory = directories[path];
    if (!directory || directory.loading) {
      return (
        <div className="files-tree-item files-tree-loading" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">正在读取...</span>
        </div>
      );
    }

    if (directory.error) {
      return (
        <div className="files-tree-error" style={{ marginLeft: `${22 + depth * 14}px` }}>
          <span>{directory.error}</span>
          <button type="button" onClick={() => void loadDirectory(path)}>
            重试
          </button>
        </div>
      );
    }

    const visibleItems = directory.items.filter(nodeMatchesSearch);

    if (directory.items.length === 0) {
      return (
        <div className="files-tree-item muted" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">空目录</span>
        </div>
      );
    }

    if (visibleItems.length === 0) {
      return (
        <div className="files-tree-item muted" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">无匹配文件</span>
        </div>
      );
    }

    return (
      <>
        {visibleItems.map((node) => renderNode(node, depth))}
        {directory.truncated ? (
          <div className="files-tree-note" style={{ marginLeft: `${22 + depth * 14}px` }}>
            当前目录项目过多，仅展示前 {directory.items.length} 项
          </div>
        ) : null}
      </>
    );
  };

  const renderNode = (node: WorkspaceFileNode, depth: number) => {
    const isDirectory = node.kind === "directory";
    const expanded = expandedPaths.has(node.path);
    return (
      <div className="files-tree-node" key={node.path}>
        <button
          type="button"
          className={`files-tree-item files-tree-row${isDirectory ? " directory" : ""}${activeFilePath === node.path ? " active" : ""}`}
          title={node.path}
          style={{ paddingLeft: `${8 + depth * 14}px` }}
          onClick={() => handleNodeClick(node)}
        >
          <span className="codicon-lite files-tree-chevron">
            {isDirectory ? (expanded ? "⌄" : "›") : ""}
          </span>
          <span className={`file-icon ${node.kind}`}>{fileIcon(node)}</span>
          <span className="file-label">{node.name}</span>
          {node.kind === "file" ? (
            <span className="files-tree-meta">{formatFileSize(node.size)}</span>
          ) : null}
        </button>
        {isDirectory && expanded ? renderDirectory(node.path, depth + 1) : null}
      </div>
    );
  };

  const rootExpanded = expandedPaths.has(ROOT_PATH);

  return (
    <div className="workspace-file-tree">
      {searchOpen ? (
        <div className="files-tree-search">
          <input
            type="search"
            value={searchQuery}
            placeholder="筛选文件"
            aria-label="筛选文件"
            autoFocus
            onChange={(event) => setSearchQuery(event.target.value)}
          />
        </div>
      ) : null}
      <div className="files-tree-root" role="tree" aria-label="工作区文件树">
        <button
          type="button"
          className="files-tree-item root files-tree-row"
          title={workspaceRoot || rootLabel}
          aria-expanded={rootExpanded}
          onClick={handleRootClick}
        >
          <span className="codicon-lite files-tree-chevron">{rootExpanded ? "⌄" : "›"}</span>
          <span className="file-icon directory">▣</span>
          <span className="file-label">{rootLabel}</span>
        </button>
        {rootExpanded ? renderDirectory(ROOT_PATH, 0) : null}
      </div>
    </div>
  );
}
