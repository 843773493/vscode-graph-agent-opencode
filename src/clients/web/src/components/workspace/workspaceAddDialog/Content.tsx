import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

import type { WorkspaceAddDialogProps } from "../WorkspaceAddDialog";
import { browseGatewayLocalDirectories } from "../../../gatewayApi";
import type { GatewayDirectoryList } from "../../../types/backend";
import {
  workspaceDirectoryMatchesQuery,
  workspacePathSearchParts,
} from "../../../utils/selection/workspaceDirectorySelection";
import { errorMessage } from "../../../utils/errorMessage";
import {
  buildWorkspaceAddGatewayChoices,
  type WorkspaceAddGatewayChoice,
} from "./gatewayChoices";

function isImeEnter(event: ReactKeyboardEvent<HTMLInputElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

export default function WorkspaceAddDialogContent({
  open,
  apiPort,
  workspaces,
  onClose,
  onOpenConnectionManager,
  onAdd,
}: WorkspaceAddDialogProps) {
  const choices = useMemo(() => buildWorkspaceAddGatewayChoices(workspaces), [workspaces]);
  const showGatewaySearch = choices.length > 4;
  const [selectedGatewayKey, setSelectedGatewayKey] = useState<string | null>(null);
  const [gatewayQuery, setGatewayQuery] = useState("");
  const [listing, setListing] = useState<GatewayDirectoryList | null>(null);
  const [currentPath, setCurrentPath] = useState("");
  const [pathInput, setPathInput] = useState("");
  const [directoryQuery, setDirectoryQuery] = useState("");
  const [selectedPath, setSelectedPath] = useState("");
  const [showHiddenDirectories, setShowHiddenDirectories] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gatewaySearchRef = useRef<HTMLInputElement | null>(null);
  const directoryRequestRef = useRef(0);

  const selectedGateway = useMemo(
    () => choices.find((choice) => choice.key === selectedGatewayKey) ?? null,
    [choices, selectedGatewayKey],
  );

  const loadDirectory = useCallback(
    async (
      gateway: WorkspaceAddGatewayChoice,
      path: string | null,
      nextQuery = "",
      basePath?: string,
    ) => {
      const requestNumber = directoryRequestRef.current + 1;
      directoryRequestRef.current = requestNumber;
      setLoading(true);
      setError(null);
      setSelectedPath("");
      try {
        const nextListing = await browseGatewayLocalDirectories(
          apiPort,
          path,
          gateway.connectionId,
          basePath,
        );
        if (directoryRequestRef.current !== requestNumber) return;
        setListing(nextListing);
        setCurrentPath(nextListing.path);
        setPathInput(nextQuery || nextListing.path);
        setDirectoryQuery(nextQuery);
        if (!nextQuery) {
          setSelectedPath(nextListing.path);
        } else {
          let uniqueMatchPath: string | null = null;
          let hasMultipleMatches = false;
          for (const entry of nextListing.entries) {
            if (!workspaceDirectoryMatchesQuery(entry.name, nextQuery)) continue;
            if (uniqueMatchPath !== null) {
              hasMultipleMatches = true;
              break;
            }
            uniqueMatchPath = entry.path;
          }
          setSelectedPath(hasMultipleMatches ? "" : uniqueMatchPath ?? "");
        }
      } catch (loadError) {
        if (directoryRequestRef.current !== requestNumber) return;
        setListing(null);
        setError(
          errorMessage(loadError),
        );
      } finally {
        if (directoryRequestRef.current === requestNumber) setLoading(false);
      }
    },
    [apiPort],
  );

  useEffect(() => {
    directoryRequestRef.current += 1;
    if (!open) return;
    setSelectedGatewayKey(null);
    setGatewayQuery("");
    setListing(null);
    setCurrentPath("");
    setPathInput("");
    setDirectoryQuery("");
    setSelectedPath("");
    setShowHiddenDirectories(false);
    setLoading(false);
    setSubmitting(false);
    setError(null);
    if (showGatewaySearch) {
      window.setTimeout(() => gatewaySearchRef.current?.focus(), 0);
    }
  }, [open, showGatewaySearch]);

  useEffect(() => {
    if (!open) return undefined;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || submitting) return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [onClose, open, submitting]);

  const filteredGateways = useMemo(() => {
    const normalizedQuery = gatewayQuery.trim().toLocaleLowerCase();
    return choices.filter((choice) =>
      `${choice.name} ${choice.detail}`
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [choices, gatewayQuery]);

  const visibleDirectories = useMemo(() => {
    const entries = [...(listing?.entries ?? [])].sort((left, right) =>
      left.name.localeCompare(right.name),
    );
    return showHiddenDirectories
      ? entries
      : entries.filter((entry) => !entry.name.startsWith("."));
  }, [listing?.entries, showHiddenDirectories]);

  const filteredDirectories = useMemo(
    () =>
      visibleDirectories.filter((entry) =>
        workspaceDirectoryMatchesQuery(entry.name, directoryQuery),
      ),
    [directoryQuery, visibleDirectories],
  );

  const selectGateway = (gateway: WorkspaceAddGatewayChoice) => {
    if (gateway.status === "offline") return;
    setSelectedGatewayKey(gateway.key);
    setListing(null);
    setCurrentPath("");
    setPathInput("");
    setDirectoryQuery("");
    setSelectedPath("");
    setShowHiddenDirectories(false);
    setError(null);
    void loadDirectory(gateway, null);
  };

  const browsePath = (path: string) => {
    if (!selectedGateway) return;
    void loadDirectory(selectedGateway, path.trim() || ".", "", currentPath);
  };

  const confirmPathInput = () => {
    if (!selectedGateway || !currentPath) return;
    const normalized = pathInput.trim();
    if (selectedPath && (normalized === currentPath || normalized === directoryQuery)) return;

    const { parentPath, query } = workspacePathSearchParts(pathInput);
    const matchingDirectories =
      !parentPath || parentPath === currentPath
        ? visibleDirectories.filter((entry) =>
            workspaceDirectoryMatchesQuery(entry.name, query),
          )
        : [];
    if (query && matchingDirectories.length === 1) {
      browsePath(matchingDirectories[0].path);
      return;
    }
    if (query) {
      void loadDirectory(selectedGateway, parentPath || currentPath, query, currentPath);
      return;
    }
    browsePath(parentPath);
  };

  const handleConfirm = async () => {
    if (!selectedGateway || submitting || !selectedPath) return;
    setSubmitting(true);
    setError(null);
    try {
      await onAdd({
        gateway_connection_id: selectedGateway.connectionId,
        root_path: selectedPath,
        create_directory: false,
      });
      onClose();
    } catch (submitError) {
      setError(
        errorMessage(submitError),
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  return createPortal(
    <div
      className="workspace-add-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <section
        className="workspace-add-panel workspace-add-two-level"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-add-title"
      >
        <header className="workspace-add-header">
          <div className="workspace-add-dialog-heading">
            {selectedGateway ? (
              <button
                type="button"
                className="workspace-add-icon-button workspace-add-back-button"
                aria-label="返回选择设备"
                disabled={submitting}
                onClick={() => {
                  directoryRequestRef.current += 1;
                  setSelectedGatewayKey(null);
                  setListing(null);
                  setError(null);
                  window.setTimeout(() => gatewaySearchRef.current?.focus(), 0);
                }}
              >
                <span className="codicon codicon-arrow-left" aria-hidden="true" />
                <span>设备</span>
              </button>
            ) : null}
            <div>
              <h2 id="workspace-add-title">
                {selectedGateway ? "选择工作区文件夹" : "选择工作区所在设备"}
              </h2>
              <p>
                {selectedGateway
                  ? `在“${selectedGateway.name}”上选择一个文件夹作为工作区。`
                  : "选择包含目标文件夹的当前电脑或远程 SSH 主机。"}
              </p>
            </div>
            <button
              type="button"
              className="workspace-add-icon-button workspace-add-close-button"
              aria-label="关闭"
              disabled={submitting}
              onClick={onClose}
            >
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          </div>

          {!selectedGateway && showGatewaySearch ? (
            <label className="workspace-add-search-field">
              <span className="codicon codicon-search" aria-hidden="true" />
              <input
                ref={gatewaySearchRef}
                type="search"
                value={gatewayQuery}
                onChange={(event) => setGatewayQuery(event.target.value)}
                placeholder="搜索电脑或远程主机"
                aria-label="搜索电脑或远程主机"
              />
            </label>
          ) : selectedGateway ? (
            <div className="workspace-add-path-toolbar">
              <button
                type="button"
                className="workspace-add-icon-button"
                aria-label="返回上级目录"
                title="返回上级目录"
                disabled={
                  loading || submitting || !listing?.parent_path
                }
                onClick={() => { if (listing?.parent_path) browsePath(listing.parent_path); }}
              >
                <span className="codicon codicon-arrow-up" aria-hidden="true" />
              </button>
              <label>
                <input
                  type="text"
                  value={pathInput}
                  disabled={loading || submitting || !currentPath}
                  onChange={(event) => {
                    setPathInput(event.target.value);
                    setSelectedPath("");
                    setError(null);
                  }}
                  onBlur={confirmPathInput}
                  onKeyDown={(event) => {
                    if (isImeEnter(event)) return;
                    if (event.key === "Enter") {
                      event.preventDefault();
                      confirmPathInput();
                    }
                  }}
                  placeholder="正在加载目录..."
                  aria-label="目录地址"
                  aria-describedby="workspace-add-path-hint"
                />
              </label>
              <label className="workspace-add-hidden-toggle">
                <input
                  type="checkbox"
                  checked={showHiddenDirectories}
                  disabled={loading || submitting}
                  onChange={(event) =>
                    setShowHiddenDirectories(event.target.checked)
                  }
                />
                显示隐藏目录
              </label>
            </div>
          ) : null}
          {selectedGateway ? (
            <small id="workspace-add-path-hint" className="workspace-add-path-hint">
              输入路径或目录名后按 Enter；单击目录进行选择，点击右侧箭头进入目录。
            </small>
          ) : null}
        </header>

        <div className="workspace-add-results" aria-busy={loading || submitting}>
          {error ? (
            <div className="workspace-add-error" role="alert">
              {error}
            </div>
          ) : null}

          {!selectedGateway ? (
            filteredGateways.length > 0 ? (
              filteredGateways.map((gateway) => (
                <button
                  key={gateway.key}
                  type="button"
                  className="workspace-add-row workspace-add-gateway-row"
                  disabled={gateway.status === "offline"}
                  onClick={() => selectGateway(gateway)}
                >
                  <span
                    className={`codicon ${
                      gateway.kind === "remote"
                        ? "codicon-server-environment"
                        : "codicon-device-desktop"
                    }`}
                    aria-hidden="true"
                  />
                  <span>
                    <strong>{gateway.name}</strong>
                    <small title={gateway.error ?? undefined}>
                      <i className={`workspace-add-status-dot ${gateway.status}`} />
                      {gateway.kind === "local" ? "本机" : gateway.detail} · {gateway.statusLabel}
                    </small>
                    {gateway.error ? (
                      <small className="workspace-add-gateway-error" title={gateway.error}>
                        {gateway.error}
                      </small>
                    ) : null}
                  </span>
                  <span className="workspace-add-gateway-count">
                    已管理 {gateway.workspaceCount} 个工作区
                  </span>
                  <span className="codicon codicon-chevron-right" aria-hidden="true" />
                </button>
              ))
            ) : choices.length === 0 ? (
              <div className="workspace-add-state">暂无可用设备</div>
            ) : (
              <div className="workspace-add-state">没有匹配的电脑或远程主机</div>
            )
          ) : loading && !listing ? (
            <div className="workspace-add-state">正在加载目录...</div>
          ) : (
            <>
              {filteredDirectories.map((entry) => {
                const selected = selectedPath === entry.path;
                return (
                  <div
                    key={entry.path}
                    className={`workspace-add-row workspace-add-directory-row${
                      selected ? " active" : ""
                    }`}
                  >
                    <button
                      type="button"
                      className="workspace-add-directory-select"
                      disabled={loading || submitting}
                      aria-pressed={selected}
                      onClick={() => setSelectedPath(entry.path)}
                      onDoubleClick={() => browsePath(entry.path)}
                    >
                      <span className="codicon codicon-folder" aria-hidden="true" />
                      <span title={entry.path}>
                        <strong>{entry.name}</strong>
                      </span>
                      {selected ? (
                        <span className="codicon codicon-check" aria-hidden="true" />
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="workspace-add-directory-enter"
                      disabled={loading || submitting}
                      aria-label={`进入目录 ${entry.name}`}
                      title="进入目录"
                      onClick={() => browsePath(entry.path)}
                    >
                      <span className="codicon codicon-chevron-right" aria-hidden="true" />
                    </button>
                  </div>
                );
              })}
              {!loading && !error && filteredDirectories.length === 0 ? (
                <div className="workspace-add-state">当前目录下没有子目录</div>
              ) : null}
              {loading && listing ? (
                <div className="workspace-add-state">正在加载目录...</div>
              ) : null}
            </>
          )}
        </div>

        {selectedGateway ? (
          <footer className="workspace-add-actions">
            <div className="workspace-add-selection-summary" aria-live="polite">
              <span>将添加</span>
              <strong title={selectedPath || undefined}>
                {selectedPath === currentPath ? listing?.name : listing?.entries.find((entry) => entry.path === selectedPath)?.name || "尚未选择文件夹"}
              </strong>
            </div>
            <div className="workspace-add-action-buttons">
              <button type="button" disabled={submitting} onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="primary"
                disabled={
                  loading || submitting || !selectedPath
                }
                onClick={() => void handleConfirm()}
              >
                {submitting ? (
                  <span
                    className="codicon codicon-loading codicon-modifier-spin"
                    aria-hidden="true"
                  />
                ) : null}
                添加工作区
              </button>
            </div>
          </footer>
        ) : (
          <footer className="workspace-add-connection-actions">
            <span>目标设备不在列表中？</span>
            <button
              type="button"
              onClick={() => {
                onClose();
                onOpenConnectionManager();
              }}
            >
              <span className="codicon codicon-plug" aria-hidden="true" />
              连接新的远程主机…
            </button>
          </footer>
        )}
      </section>
    </div>,
    document.body,
  );
}
