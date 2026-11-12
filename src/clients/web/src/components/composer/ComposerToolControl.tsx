import { t } from "../../../../shared/core/i18n/index.js";
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getToolCatalog,
  getToolTestRun,
  listToolTestRuns,
  startToolTest,
  updateToolSelection,
} from "../../api";
import type {
  ToolCatalogItem,
  ToolKind,
  ToolSelectionChange,
  ToolTestRun,
} from "../../types/toolTesting";
import ComposerToolTree, { type ToolGroup } from "./ComposerToolTree";
import AnchoredOverlay from "../overlays/AnchoredOverlay";
import {
  applyToolSelectionChanges,
  restoreToolSelectionAfterSaveFailure,
} from "./toolSelectionState";
import { errorMessage } from "../../utils/errorMessage";

const TOOL_GROUP_KIND_ORDER: Record<ToolKind, number> = {
  default: 0,
  collaboration: 1,
  extension: 2,
  debugging: 3,
};

function latestRunsByTool(runs: ToolTestRun[]): Map<string, ToolTestRun> {
  const result = new Map<string, ToolTestRun>();
  for (const run of runs) {
    if (!result.has(run.tool_name)) {
      result.set(run.tool_name, run);
    }
  }
  return result;
}

export default function ComposerToolControl({
  apiPort,
  agentId,
  workspaceId,
  onStatus,
}: {
  apiPort: number;
  agentId: string;
  workspaceId: string | null;
  onStatus: (text: string) => void;
}): React.ReactNode {
  const [open, setOpen] = useState(false);
  const [tools, setTools] = useState<ToolCatalogItem[]>([]);
  const [runs, setRuns] = useState<Map<string, ToolTestRun>>(new Map());
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [savingToolIds, setSavingToolIds] = useState<Set<string>>(new Set());
  const [testingTools, setTestingTools] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const controlRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  // 加载归属代次：agentId/workspaceId 变化会重新加载，旧请求晚到时必须被丢弃，
  // 否则界面会稳定显示上一个 Agent 的工具开关。
  const loadGenerationRef = useRef(0);
  // 轮询归属作用域：测试进度请求带着发起时的 owner，切换 Agent 或工作区后
  // 迟到结果不得写回，也不能再用新 owner 去轮询上一个 owner 的 run_id。
  const pollScopeKey = `${apiPort}:${agentId}:${workspaceId ?? ""}`;
  const pollScopeKeyRef = useRef(pollScopeKey);
  pollScopeKeyRef.current = pollScopeKey;

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [catalog, history] = await Promise.all([
        getToolCatalog(apiPort, agentId, workspaceId),
        listToolTestRuns(apiPort, workspaceId),
      ]);
      if (loadGenerationRef.current !== generation) return;
      setTools(catalog);
      setRuns(latestRunsByTool(history));
    } catch (loadError) {
      // 失败始终向上抛出，不静默吞掉；只有当前代次才写本地错误态。
      if (loadGenerationRef.current === generation) {
        setError(errorMessage(loadError));
      }
      throw loadError;
    } finally {
      if (loadGenerationRef.current === generation) setLoading(false);
    }
  }, [agentId, apiPort, workspaceId]);

  useEffect(() => {
    if (!open) {
      return;
    }
    void load().catch(() => {
      onStatus(t("composer.tools.loadFailed"));
    });
  }, [load, onStatus, open]);

  useEffect(() => {
    if (testingTools.size === 0) {
      return;
    }
    const requestScopeKey = pollScopeKey;
    const timer = window.setInterval(() => {
      for (const toolName of testingTools) {
        const run = runs.get(toolName);
        if (!run) {
          continue;
        }
        void getToolTestRun(apiPort, run.run_id, workspaceId)
          .then((nextRun) => {
            if (pollScopeKeyRef.current !== requestScopeKey) return;
            setRuns((current) => new Map(current).set(toolName, nextRun));
            if (nextRun.status === "completed" || nextRun.status === "failed") {
              setTestingTools((current) => {
                const next = new Set(current);
                next.delete(toolName);
                return next;
              });
            }
          })
          .catch((pollError: unknown) => {
            if (pollScopeKeyRef.current !== requestScopeKey) return;
            setError(t("composer.tools.progressFailed", errorMessage(pollError)));
          });
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [apiPort, runs, testingTools, workspaceId, pollScopeKey]);

  useEffect(() => {
    // owner 变化后旧的进行中测试不再属于当前上下文：清空以免用新 owner 复查旧 run。
    setTestingTools((current) => (current.size === 0 ? current : new Set()));
  }, [pollScopeKey]);

  const groups = useMemo<ToolGroup[]>(() => {
    const byId = new Map<string, ToolGroup>();
    for (const tool of tools) {
      const current = byId.get(tool.group_id);
      if (current) {
        current.items.push(tool);
      } else {
        byId.set(tool.group_id, {
          id: tool.group_id,
          name: tool.group_name,
          kind: tool.kind,
          items: [tool],
        });
      }
    }
    return [...byId.values()].sort((left, right) => {
      if (left.id === "default") return -1;
      if (right.id === "default") return 1;
      const kindOrder = TOOL_GROUP_KIND_ORDER[left.kind]
        - TOOL_GROUP_KIND_ORDER[right.kind];
      if (kindOrder !== 0) return kindOrder;
      return left.name.localeCompare(right.name);
    });
  }, [tools]);

  const saveChanges = async (
    changes: ToolSelectionChange[],
    successMessage: string,
  ) => {
    const previousTools = tools;
    const changedIds = new Set(changes.map((change) => change.tool_id));
    setSavingToolIds((current) => new Set([...current, ...changedIds]));
    setTools((current) => applyToolSelectionChanges(current, changes));
    setError(null);
    try {
      const updatedTools = await updateToolSelection(
        apiPort,
        agentId,
        changes,
        workspaceId,
      );
      const updatedById = new Map(
        updatedTools.map((tool) => [tool.tool_id, tool]),
      );
      setTools((current) => current.map(
        (tool) => updatedById.get(tool.tool_id) ?? tool,
      ));
      onStatus(successMessage);
    } catch (saveError) {
      const message = errorMessage(saveError);
      setError(t("composer.tools.saveFailedDetail", message));
      try {
        const refreshedTools = await getToolCatalog(apiPort, agentId, workspaceId);
        setTools(restoreToolSelectionAfterSaveFailure(previousTools, refreshedTools));
      } catch (refreshError) {
        setTools(restoreToolSelectionAfterSaveFailure(previousTools, null));
        const refreshMessage = errorMessage(refreshError);
        setError(t("composer.tools.saveAndRefreshFailed", message, refreshMessage));
      }
      throw saveError;
    } finally {
      setSavingToolIds((current) => {
        const next = new Set(current);
        for (const toolId of changedIds) {
          next.delete(toolId);
        }
        return next;
      });
    }
  };

  const toggleToolExecution = (toolId: string) => {
    const tool = tools.find((item) => item.tool_id === toolId);
    if (!tool || savingToolIds.has(toolId)) {
      return;
    }
    void saveChanges(
      [{
        tool_id: toolId,
        execution_enabled: !tool.execution_enabled,
        model_visible: tool.execution_enabled ? false : tool.model_visible,
      }],
      t("composer.tools.saved", tool.name),
    ).catch(() => {
      onStatus(t("composer.tools.saveFailed"));
    });
  };

  const toggleToolModelVisibility = (toolId: string) => {
    const tool = tools.find((item) => item.tool_id === toolId);
    if (!tool || savingToolIds.has(toolId) || !tool.execution_enabled) {
      return;
    }
    void saveChanges(
      [{
        tool_id: toolId,
        execution_enabled: true,
        model_visible: !tool.model_visible,
      }],
      t("composer.tools.visibilitySaved", tool.name),
    ).catch(() => {
      onStatus(t("composer.tools.saveFailed"));
    });
  };

  const toggleGroupCapability = (
    group: ToolGroup,
    capability: "execution" | "model",
  ) => {
    if (group.items.some((tool) => savingToolIds.has(tool.tool_id))) {
      return;
    }
    const enableGroup = capability === "execution"
      ? group.items.some((tool) => !tool.execution_enabled)
      : group.items.some(
          (tool) => tool.execution_enabled && !tool.model_visible,
        );
    const changes = group.items.map((tool) => ({
      tool_id: tool.tool_id,
      execution_enabled: capability === "execution"
        ? enableGroup
        : tool.execution_enabled,
      model_visible: capability === "execution"
        ? (enableGroup ? tool.model_visible : false)
        : (tool.execution_enabled && enableGroup),
    }));
    void saveChanges(changes, t("composer.tools.groupSaved", group.name)).catch(() => {
      onStatus(t("composer.tools.groupSaveFailed"));
    });
  };

  const runTest = (tool: ToolCatalogItem) => {
    setError(null);
    setTestingTools((current) => new Set(current).add(tool.tool_id));
    void startToolTest(apiPort, tool.tool_id, agentId, workspaceId)
      .then((run) => {
        setRuns((current) => new Map(current).set(tool.tool_id, run));
        onStatus(t("composer.tools.testStarted", tool.name));
      })
      .catch((testError: unknown) => {
        const message = errorMessage(testError);
        setError(t("composer.tools.testStartFailed", message));
        setTestingTools((current) => {
          const next = new Set(current);
          next.delete(tool.tool_id);
          return next;
        });
      });
  };

  const executionCount = tools.filter((tool) => tool.execution_enabled).length;
  const modelVisibleCount = tools.filter((tool) => tool.model_visible).length;

  return (
    <div className="composer-tool-control" ref={controlRef}>
      <button
        ref={buttonRef}
        type="button"
        className={`composer-icon-button composer-tool-button${open ? " active" : ""}`}
        title={t("composer.tools.select")}
        aria-label={t("composer.tools.select")}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="codicon codicon-settings" aria-hidden="true" />
      </button>
      <AnchoredOverlay
        open={open}
        anchorRef={buttonRef}
        placement="top-end"
        onClose={() => setOpen(false)}
      >
        <section
          className="composer-tool-menu"
          aria-label={t("composer.tools.menu")}
        >
          <header className="composer-tool-menu-header">
            <div>
              <strong>{t("composer.tools.title")}</strong>
              <span>
                {loading
                  ? t("composer.tools.loading")
                  : t("composer.tools.count", executionCount, tools.length, modelVisibleCount)}
              </span>
            </div>
            <div className="composer-tool-menu-header-actions">
              <button
                type="button"
                className="composer-tool-refresh"
                title={t("composer.tools.refresh")}
                aria-label={t("composer.tools.refresh")}
                disabled={loading}
                onClick={() => void load()}
              >
                <span className="codicon codicon-refresh" aria-hidden="true" />
              </button>
              <button
                type="button"
                className="composer-tool-close"
                title={t("composer.tools.close")}
                aria-label={t("composer.tools.close")}
                onClick={() => setOpen(false)}
              >
                <span className="codicon codicon-close" aria-hidden="true" />
              </button>
            </div>
          </header>
          {error ? <div className="composer-tool-error">{error}</div> : null}
          <ComposerToolTree
            groups={groups}
            loading={loading}
            savingToolIds={savingToolIds}
            runs={runs}
            testingTools={testingTools}
            collapsedGroups={collapsedGroups}
            onToggleCollapsed={(groupId) => setCollapsedGroups((current) => {
              const next = new Set(current);
              if (next.has(groupId)) next.delete(groupId);
              else next.add(groupId);
              return next;
            })}
            onToggleGroupCapability={toggleGroupCapability}
            onToggleToolExecution={toggleToolExecution}
            onToggleToolModelVisibility={toggleToolModelVisibility}
            onRunTest={runTest}
          />
        </section>
      </AnchoredOverlay>
    </div>
  );
}
