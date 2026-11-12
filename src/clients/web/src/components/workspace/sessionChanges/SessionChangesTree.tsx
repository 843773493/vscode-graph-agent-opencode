import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import type {
  SessionChangeset,
  SessionChangesetListItem,
  SessionFileChange,
} from "../../../types/backend";
import { formatDateTime } from "../../../utils/format";

function formatSummary(summary: { files: number; additions: number; deletions: number }) {
  return t("changes.summary", formatNumber(summary.files), formatNumber(summary.additions), formatNumber(summary.deletions));
}

function changeKindIcon(kind: SessionFileChange["kind"]) {
  if (kind === "create") {
    return "codicon-diff-added";
  }
  if (kind === "delete") {
    return "codicon-diff-removed";
  }
  return "codicon-diff-modified";
}

function changeKindLabel(kind: SessionFileChange["kind"]) {
  if (kind === "create") {
    return t("changes.create");
  }
  if (kind === "delete") {
    return t("changes.delete");
  }
  return t("changes.modify");
}

export default function SessionChangesTree({
  changesets,
  selectedChangesetId,
  activeChangeset,
  loading,
  error,
  loadedAt,
  onSelectChangeset,
  onRefresh,
  onOpenFile,
  onReviewFile,
}: {
  changesets: SessionChangesetListItem[];
  selectedChangesetId: string | null;
  activeChangeset: SessionChangeset | null;
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  onSelectChangeset: (changesetId: string) => void;
  onRefresh: () => void;
  onOpenFile: (file: SessionFileChange) => void;
  onReviewFile: (file: SessionFileChange, reviewed: boolean) => Promise<void>;
}) {
  const summary = activeChangeset?.summary ?? { files: 0, additions: 0, deletions: 0 };

  return (
    <div className="session-changes-tree">
      <div className="auxiliary-actions-row">
        <button type="button" onClick={onRefresh} disabled={loading}>

          {t("changes.refresh")}
        </button>
        {loadedAt ? (
          <span className="session-changes-loaded">{t("changes.readAt", formatDateTime(loadedAt))}</span>
        ) : null}
      </div>

      <div className="auxiliary-change-summary auxiliary-change-summary-hero">
        <span className="auxiliary-change-stat added">+{formatNumber(summary.additions)}</span>
        <span className="auxiliary-change-stat removed">-{formatNumber(summary.deletions)}</span>
        <span className="auxiliary-change-muted">
          {summary.files > 0 ? t("changes.count", formatNumber(summary.files)) : t("changes.none")}
        </span>
      </div>

      {loading ? <div className="auxiliary-empty-row">{t("changes.loading")}</div> : null}
      {error ? <div className="auxiliary-empty-row danger">{error}</div> : null}

      <section className="auxiliary-tree-section">
        <header>{t("changes.sets")}</header>
        {changesets.length > 0 ? (
          <div className="session-changeset-list">
            {changesets.map((changeset) => (
              <button
                type="button"
                key={changeset.changeset_id}
                className={`session-changeset-row ${
                  changeset.changeset_id === selectedChangesetId ? "active" : ""
                }`}
                aria-pressed={changeset.changeset_id === selectedChangesetId}
                title={changeset.description ?? changeset.label}
                onClick={() => onSelectChangeset(changeset.changeset_id)}
              >
                <span>{changeset.label}</span>
                <span>{formatSummary(changeset.summary)}</span>
              </button>
            ))}
          </div>
        ) : !loading && !error ? (
          <div className="auxiliary-empty-row muted">{t("changes.empty")}</div>
        ) : null}
      </section>

      <section className="auxiliary-tree-section">
        <header>{t("changes.files")}</header>
        {activeChangeset && activeChangeset.files.length > 0 ? (
          <div className="session-change-file-list">
            {activeChangeset.files.map((file) => (
              <div className="session-change-file-row" key={file.file_path}>
                <button
                  type="button"
                  className="session-change-file-main"
                  title={file.file_path}
                  onClick={() => onOpenFile(file)}
                >
                  <span className={`codicon ${changeKindIcon(file.kind)}`} aria-hidden="true" />
                  <span className="session-change-file-text">
                    <span>{file.file_path}</span>
                    <span>
                      {changeKindLabel(file.kind)} · +{formatNumber(file.additions)} -{formatNumber(file.deletions)}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  className={`session-change-review-button ${file.reviewed ? "reviewed" : ""}`}
                  title={file.reviewed ? t("changes.unreview") : t("changes.review")}
                  aria-label={file.reviewed ? t("changes.unreview") : t("changes.review")}
                  onClick={() => {
                    void onReviewFile(file, !file.reviewed);
                  }}
                >
                  <span
                    className={`codicon ${
                      file.reviewed ? "codicon-pass-filled" : "codicon-pass"
                    }`}
                    aria-hidden="true"
                  />
                </button>
              </div>
            ))}
          </div>
        ) : !loading && !error ? (
          <div className="auxiliary-empty-row muted">{t("changes.noFiles")}</div>
        ) : null}
      </section>
    </div>
  );
}
