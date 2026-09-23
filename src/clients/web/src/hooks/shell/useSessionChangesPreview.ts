import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceAuxiliaryTab } from "../../components/workspace/WorkspaceAuxiliaryPanel";
import type {
  SessionChangesSummary,
  SessionChangesetList,
  SessionChangeset,
  SessionFileChange,
  Session,
} from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";
import type { SessionTurnTimeline } from "../../state/session/turnTimeline";
import { shouldLoadDefaultViewChangesHint } from "../../state/defaultViewChanges";
import { errorMessage } from "../../utils/errorMessage";

export interface SessionChangesHint {
  sessionId: string;
  summary: SessionChangesSummary;
}

interface UseSessionChangesPreviewInput {
  activeSession: Session | null;
  /** 当前会话所属工作区；会话文件变更按工作区分批，切换工作区必须重新评估提示。 */
  activeSessionWorkspaceId: string | null;
  contentView: ConversationContentView;
  activeChangeset: SessionChangeset | null;
  sessionChangesLoading: boolean;
  sessionChangesError: string | null;
  /** 后端权威设置里的右侧栏可见性；刷新恢复时显式 false 优先于上次内容视图。 */
  layoutAuxiliaryVisible: boolean | undefined;
  auxiliaryVisible: boolean;
  auxiliaryTab: WorkspaceAuxiliaryTab;
  activeTurnTimeline: SessionTurnTimeline | null;
  conversationCount: number;
  activePreviewPath: string | null;
  loadSessionChangesets: (sessionId: string) => Promise<SessionChangesetList>;
  switchContentView: (view: ConversationContentView) => void;
  setAuxiliaryVisible: (visible: boolean) => void;
  setStatus: (text: string) => void;
  openSessionChangePreview: (
    changeset: SessionChangeset,
    file: SessionFileChange,
  ) => void;
}

/**
 * 会话文件变更的展示编排链路：默认视图下的变更提示摘要、变更视图的侧边栏联动，
 * 以及打开变更预览时的去重。变更数据本身由 useContentViewEffects 统一拉取，
 * 这里只做展示态编排，避免同一份变更被两条 effect 同时读取。
 */
export function useSessionChangesPreview({
  activeSession,
  activeSessionWorkspaceId,
  contentView,
  activeChangeset,
  sessionChangesLoading,
  sessionChangesError,
  layoutAuxiliaryVisible,
  auxiliaryVisible,
  auxiliaryTab,
  activeTurnTimeline,
  conversationCount,
  activePreviewPath,
  loadSessionChangesets,
  switchContentView,
  setAuxiliaryVisible,
  setStatus,
  openSessionChangePreview,
}: UseSessionChangesPreviewInput) {
  const [changesHint, setChangesHint] = useState<SessionChangesHint | null>(null);
  const [changesHintLoading, setChangesHintLoading] = useState(false);
  const lastOpenedPreviewKeyRef = useRef<string | null>(null);

  useEffect(() => {
    const activeSessionId = activeSession?.session_id ?? null;
    if (!activeSessionId || contentView !== "default") {
      setChangesHint(null);
      setChangesHintLoading(false);
      return;
    }

    if (auxiliaryVisible && auxiliaryTab === "changes") {
      if (activeChangeset?.session_id === activeSessionId) {
        setChangesHint({
          sessionId: activeSessionId,
          summary: activeChangeset.summary,
        });
        setChangesHintLoading(false);
      } else {
        setChangesHintLoading(sessionChangesLoading);
      }
      return;
    }

    if (!shouldLoadDefaultViewChangesHint({
      contentView,
      sessionId: activeSessionId,
      timeline: activeTurnTimeline,
      conversationCount,
    })) {
      setChangesHint(null);
      setChangesHintLoading(false);
      return;
    }

    let cancelled = false;
    setChangesHintLoading(true);
    const timerId = window.setTimeout(() => {
      void loadSessionChangesets(activeSessionId)
        .then((list) => {
          if (cancelled) {
            return;
          }
          const summary =
            list.items.find((item) => item.is_default)?.summary ??
            list.items[0]?.summary ??
            { files: 0, additions: 0, deletions: 0 };
          setChangesHint({ sessionId: activeSessionId, summary });
        })
        .catch((error: unknown) => {
          if (cancelled) {
            return;
          }
          setChangesHint(null);
          setStatus(`会话文件变更提示加载失败: ${errorMessage(error)}`);
        })
        .finally(() => {
          if (!cancelled) {
            setChangesHintLoading(false);
          }
        });
    }, 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
    };
  }, [
    activeSession?.session_id,
    activeSessionWorkspaceId,
    activeTurnTimeline,
    conversationCount,
    loadSessionChangesets,
    auxiliaryTab,
    auxiliaryVisible,
    setStatus,
    activeChangeset,
    contentView,
    sessionChangesLoading,
  ]);

  useEffect(() => {
    if (contentView !== "changes") {
      return;
    }
    // 刷新恢复页面设置时，显式隐藏右侧栏的选择必须优先于上次内容视图。
    // 用户重新打开右侧栏后，updateUiSettings 返回的新设置会移除此条件。
    if (layoutAuxiliaryVisible !== false) {
      setAuxiliaryVisible(true);
    }
  }, [contentView, layoutAuxiliaryVisible, setAuxiliaryVisible]);

  useEffect(() => {
    if (contentView !== "changes") {
      return;
    }
    if (!activeChangeset || activeChangeset.files.length === 0) {
      return;
    }

    const activeDiffFile = activeChangeset.files.find(
      (file) =>
        activePreviewPath ===
        `session-diff://${activeChangeset.changeset_id}/${encodeURIComponent(file.file_path)}`,
    );
    const targetFile = activeDiffFile ?? activeChangeset.files[0];
    const key = `${activeChangeset.changeset_id}:${targetFile.file_path}:${targetFile.reviewed}`;
    if (lastOpenedPreviewKeyRef.current === key) {
      return;
    }
    lastOpenedPreviewKeyRef.current = key;
    openSessionChangePreview(activeChangeset, targetFile);
  }, [
    activePreviewPath,
    activeChangeset,
    contentView,
    openSessionChangePreview,
  ]);

  useEffect(() => {
    const activeSessionId = activeSession?.session_id ?? null;
    if (
      !activeSessionId ||
      !auxiliaryVisible ||
      auxiliaryTab !== "changes" ||
      contentView === "changes"
    ) {
      return;
    }
    if (sessionChangesLoading || sessionChangesError) {
      return;
    }
    if (activeChangeset?.session_id === activeSessionId) {
      return;
    }
    const timerId = window.setTimeout(() => {
      // 只切换到变更视图；请求由 useContentViewEffects 统一发起，
      // 避免右侧栏 effect 和内容视图 effect 同时读取同一份变更。
      void switchContentView("changes");
    }, 120);
    return () => window.clearTimeout(timerId);
  }, [
    activeSession,
    auxiliaryTab,
    auxiliaryVisible,
    switchContentView,
    activeChangeset,
    contentView,
    sessionChangesError,
    sessionChangesLoading,
  ]);

  const openChangesetFileInPreview = useCallback(
    (file: SessionFileChange) => {
      if (!activeChangeset) {
        return;
      }
      lastOpenedPreviewKeyRef.current =
        `${activeChangeset.changeset_id}:${file.file_path}:${file.reviewed}`;
      openSessionChangePreview(activeChangeset, file);
    },
    [activeChangeset, openSessionChangePreview],
  );

  return {
    changesHint,
    changesHintLoading,
    openChangesetFileInPreview,
  };
}
