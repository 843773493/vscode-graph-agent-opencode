import {
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import { OverlayRootContext } from "../../overlays/OverlayRootContext";
import { t } from "../../../../../shared/core/i18n/index.js";

import type { WorkspaceAddDialogProps } from "../WorkspaceAddDialog";
import { browseGatewayLocalDirectories } from "../../../gatewayApi";
import type { GatewayDirectoryList } from "../../../types/backend";
import {
  workspaceDirectoryMatchesQuery,
  workspacePathSearchParts,
} from "../../../utils/selection/workspaceDirectorySelection";
import { errorMessage } from "../../../utils/errorMessage";
import { HttpRequestError } from "../../../api/http";
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
  const overlayRoot = useContext(OverlayRootContext);
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
        const message = loadError instanceof HttpRequestError && loadError.requestId
          ? `${errorMessage(loadError)} · request_id=${loadError.requestId}` : errorMessage(loadError);
        setError(t("gateway.workspaceDetails.directoryFailed", gateway.name, gateway.connectionId ?? "local", message));
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
      const message = submitError instanceof HttpRequestError && submitError.requestId
        ? `${errorMessage(submitError)} · request_id=${submitError.requestId}` : errorMessage(submitError);
      setError(t("gateway.workspaceDetails.directoryFailed", selectedGateway.name, selectedGateway.connectionId ?? "local", message));
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
                aria-label={t("gateway.workspaceAdd.backToDevices")}
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
                <span>{t("gateway.workspaceAdd.device")}</span>
              </button>
            ) : null}
            <div>
              <h2 id="workspace-add-title">
                {selectedGateway ? t("gateway.workspaceAdd.selectFolder") : t("gateway.workspaceAdd.selectDevice")}
              </h2>
              <p>
                {selectedGateway
                  ? t("gateway.workspaceAdd.selectFolderOnDevice", selectedGateway.name)
                  : t("gateway.workspaceAdd.selectDeviceDescription")}
              </p>
            </div>
            <button
              type="button"
              className="workspace-add-icon-button workspace-add-close-button"
              aria-label={t("gateway.workspaceAdd.close")}
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
                placeholder={t("gateway.workspaceAdd.searchDevices")}
                aria-label={t("gateway.workspaceAdd.searchDevices")}
              />
            </label>
          ) : selectedGateway ? (
            <div className="workspace-add-path-toolbar">
              <button
                type="button"
                className="workspace-add-icon-button"
                aria-label={t("gateway.workspaceAdd.parentDirectory")}
                title={t("gateway.workspaceAdd.parentDirectory")}
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
                  placeholder={t("gateway.workspaceAdd.directoryLoading")}
                  aria-label={t("gateway.workspaceAdd.directoryAddress")}
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
                {t("gateway.workspaceAdd.showHiddenDirectories")}
              </label>
            </div>
          ) : null}
          {selectedGateway ? (
            <small id="workspace-add-path-hint" className="workspace-add-path-hint">
              {t("gateway.workspaceAdd.pathHint")}
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
                      {gateway.kind === "local" ? t("gateway.workspaceAdd.localDevice") : gateway.detail} · {gateway.statusLabel}
                    </small>
                    {gateway.error ? (
                      <small className="workspace-add-gateway-error" title={gateway.error}>
                        {gateway.error}
                      </small>
                    ) : null}
                  </span>
                  <span className="workspace-add-gateway-count">
                    {t("gateway.workspaceAdd.managedWorkspaceCount", gateway.workspaceCount)}
                  </span>
                  <span className="codicon codicon-chevron-right" aria-hidden="true" />
                </button>
              ))
            ) : choices.length === 0 ? (
              <div className="workspace-add-state">{t("gateway.workspaceAdd.noDevices")}</div>
            ) : (
              <div className="workspace-add-state">{t("gateway.workspaceAdd.noMatchingDevices")}</div>
            )
          ) : loading && !listing ? (
            <div className="workspace-add-state">{t("gateway.workspaceAdd.directoryLoading")}</div>
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
                      aria-label={t("gateway.workspaceAdd.enterDirectory", entry.name)}
                      title={t("gateway.workspaceAdd.enterDirectoryTitle")}
                      onClick={() => browsePath(entry.path)}
                    >
                      <span className="codicon codicon-chevron-right" aria-hidden="true" />
                    </button>
                  </div>
                );
              })}
              {!loading && !error && filteredDirectories.length === 0 ? (
                <div className="workspace-add-state">{t("gateway.workspaceAdd.noSubdirectories")}</div>
              ) : null}
              {loading && listing ? (
                <div className="workspace-add-state">{t("gateway.workspaceAdd.directoryLoading")}</div>
              ) : null}
            </>
          )}
        </div>

        {selectedGateway ? (
          <footer className="workspace-add-actions">
            <div className="workspace-add-selection-summary" aria-live="polite">
              <span>{t("gateway.workspaceAdd.willAdd")}</span>
              <strong title={selectedPath || undefined}>
                {selectedPath === currentPath ? listing?.name : listing?.entries.find((entry) => entry.path === selectedPath)?.name || t("gateway.workspaceAdd.noFolderSelected")}
              </strong>
            </div>
            <div className="workspace-add-action-buttons">
              <button type="button" disabled={submitting} onClick={onClose}>
                {t("gateway.workspaceAdd.cancel")}
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
                {t("gateway.workspaceAdd.addWorkspace")}
              </button>
            </div>
          </footer>
        ) : (
          <footer className="workspace-add-connection-actions">
            <span>{t("gateway.workspaceAdd.deviceNotListed")}</span>
            <button
              type="button"
              onClick={() => {
                onClose();
                onOpenConnectionManager();
              }}
            >
              <span className="codicon codicon-plug" aria-hidden="true" />
              {t("gateway.workspaceAdd.connectRemoteHost")}
            </button>
          </footer>
        )}
      </section>
    </div>,
    overlayRoot ?? document.body,
  );
}
