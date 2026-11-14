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
import type { Agent } from "../../types/backend";
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
  inline = false,
  providers,
  active = true,
}: {
  apiPort: number;
  agentId: string;
  workspaceId: string | null;
  onStatus: (text: string) => void;
  inline?: boolean;
  providers: Agent["providers"];
  active?: boolean;
}): React.ReactNode {
  const [open, setOpen] = useState(inline);
  const observationActive = open && active;
  const [tools, setTools] = useState<ToolCatalogItem[]>([]);
  const [runs, setRuns] = useState<Map<string, ToolTestRun>>(new Map());
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [savingToolIds, setSavingToolIds] = useState<Set<string>>(new Set());
  const [testingTools, setTestingTools] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [testDialog, setTestDialog] = useState<ToolCatalogItem | null>(null);
  const [selectedProviders, setSelectedProviders] = useState<string[]>([]);
  const [repetitions, setRepetitions] = useState(0);
  const [testCostAcknowledged, setTestCostAcknowledged] = useState(false);
  const controlRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const mountedRef = useRef(false);
  const runsRef = useRef(runs);
  runsRef.current = runs;
  // 加载归属代次：agentId/workspaceId 变化会重新加载，旧请求晚到时必须被丢弃，
  // 否则界面会稳定显示上一个 Agent 的工具开关。
  const loadGenerationRef = useRef(0);
  // 轮询归属作用域：测试进度请求带着发起时的 owner，切换 Agent 或工作区后
  // 迟到结果不得写回，也不能再用新 owner 去轮询上一个 owner 的 run_id。
  const pollScopeKey = `${apiPort}:${agentId}:${workspaceId ?? ""}`;
  const pollScopeKeyRef = useRef(pollScopeKey);
  pollScopeKeyRef.current = pollScopeKey;
  const observeRunsRef = useRef(observationActive);
  observeRunsRef.current = observationActive;
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  useEffect(() => {
    if (!observationActive) setTestingTools((current) => current.size ? new Set() : current);
  }, [observationActive]);

  const load = useCallback(async () => {
    const requestScopeKey = pollScopeKey;
    const generation = ++loadGenerationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [catalog, history] = await Promise.all([
        getToolCatalog(apiPort, agentId, workspaceId),
        listToolTestRuns(apiPort, workspaceId),
      ]);
      if (loadGenerationRef.current !== generation || pollScopeKeyRef.current !== requestScopeKey
        || !observeRunsRef.current || !mountedRef.current) return;
      setTools(catalog);
      const latestRuns = latestRunsByTool(history);
      runsRef.current = latestRuns;
      setRuns(latestRuns);
      setTestingTools(new Set([...latestRuns.values()]
        .filter((run) => run.status === "queued" || run.status === "running")
        .map((run) => run.tool_name)));
    } catch (loadError) {
      // 失败始终向上抛出，不静默吞掉；只有当前代次才写本地错误态。
      if (loadGenerationRef.current === generation && pollScopeKeyRef.current === requestScopeKey
        && observeRunsRef.current && mountedRef.current) {
        setError(errorMessage(loadError));
      }
      throw loadError;
    } finally {
      if (loadGenerationRef.current === generation && pollScopeKeyRef.current === requestScopeKey
        && mountedRef.current) setLoading(false);
    }
  }, [agentId, apiPort, pollScopeKey, workspaceId]);

  useEffect(() => {
    if (!observationActive) {
      return;
    }
    const requestScopeKey = pollScopeKey;
    void load().catch(() => {
      if (mountedRef.current && observeRunsRef.current && pollScopeKeyRef.current === requestScopeKey) {
        onStatus(t("composer.tools.loadFailed"));
      }
    });
  }, [load, onStatus, observationActive]);

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
            if (!mountedRef.current || !observeRunsRef.current || pollScopeKeyRef.current !== requestScopeKey
              || runsRef.current.get(toolName)?.run_id !== run.run_id) return;
            const nextRuns = new Map(runsRef.current).set(toolName, nextRun);
            runsRef.current = nextRuns;
            setRuns(nextRuns);
            if (nextRun.status === "completed" || nextRun.status === "failed") {
              setTestingTools((current) => {
                const next = new Set(current);
                next.delete(toolName);
                return next;
              });
            }
          })
          .catch((pollError: unknown) => {
            if (!mountedRef.current || !observeRunsRef.current || pollScopeKeyRef.current !== requestScopeKey
              || runsRef.current.get(toolName)?.run_id !== run.run_id) return;
            setError(t("composer.tools.progressFailed", errorMessage(pollError)));
          });
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [apiPort, runs, testingTools, workspaceId, pollScopeKey]);

  useEffect(() => {
    // owner 变化后旧的进行中测试不再属于当前上下文：清空以免用新 owner 复查旧 run。
    setTestingTools((current) => (current.size === 0 ? current : new Set()));
    setTools([]);
    const nextRuns = new Map<string, ToolTestRun>();
    runsRef.current = nextRuns;
    setRuns(nextRuns);
    setTestDialog(null);
    setSavingToolIds((current) => current.size === 0 ? current : new Set());
    setError(null);
    if (!observationActive) setLoading(false);
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
    const requestScopeKey = pollScopeKey;
    const isCurrentOwner = () => mountedRef.current && pollScopeKeyRef.current === requestScopeKey;
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
      if (!isCurrentOwner()) return;
      const updatedById = new Map(
        updatedTools.map((tool) => [tool.tool_id, tool]),
      );
      setTools((current) => isCurrentOwner() ? current.map((tool) => updatedById.get(tool.tool_id) ?? tool) : current);
      onStatus(successMessage);
    } catch (saveError) {
      if (!isCurrentOwner()) throw saveError;
      const message = errorMessage(saveError);
      setError(t("composer.tools.saveFailedDetail", message));
      try {
        const refreshedTools = await getToolCatalog(apiPort, agentId, workspaceId);
        if (isCurrentOwner()) setTools(restoreToolSelectionAfterSaveFailure(previousTools, refreshedTools));
      } catch (refreshError) {
        if (isCurrentOwner()) {
          setTools(restoreToolSelectionAfterSaveFailure(previousTools, null));
          const refreshMessage = errorMessage(refreshError);
          setError(t("composer.tools.saveAndRefreshFailed", message, refreshMessage));
        }
      }
      throw saveError;
    } finally {
      if (isCurrentOwner()) {
        setSavingToolIds((current) => {
          const next = new Set(current);
          for (const toolId of changedIds) {
            next.delete(toolId);
          }
          return next;
        });
      }
    }
  };

  const toggleToolExecution = (toolId: string) => {
    const tool = tools.find((item) => item.tool_id === toolId);
    if (!tool || savingToolIds.has(toolId) || tool.execution_locked) {
      return;
    }
    const requestScopeKey = pollScopeKey;
    void saveChanges(
      [{
        tool_id: toolId,
        execution_enabled: !tool.execution_enabled,
        model_visible: tool.execution_enabled ? false : tool.model_visible,
      }],
      t("composer.tools.saved", tool.name),
    ).catch(() => {
      if (mountedRef.current && pollScopeKeyRef.current === requestScopeKey) onStatus(t("composer.tools.saveFailed"));
    });
  };

  const toggleToolModelVisibility = (toolId: string) => {
    const tool = tools.find((item) => item.tool_id === toolId);
    if (!tool || savingToolIds.has(toolId) || tool.model_visibility_locked || !tool.execution_enabled) {
      return;
    }
    const requestScopeKey = pollScopeKey;
    void saveChanges(
      [{
        tool_id: toolId,
        execution_enabled: true,
        model_visible: !tool.model_visible,
      }],
      t("composer.tools.visibilitySaved", tool.name),
    ).catch(() => {
      if (mountedRef.current && pollScopeKeyRef.current === requestScopeKey) onStatus(t("composer.tools.saveFailed"));
    });
  };

  const toggleGroupCapability = (
    group: ToolGroup,
    capability: "execution" | "model",
  ) => {
    const editableItems = group.items.filter((tool) => capability === "execution"
      ? !tool.execution_locked : !tool.model_visibility_locked);
    if (editableItems.length === 0 || editableItems.some((tool) => savingToolIds.has(tool.tool_id))) {
      return;
    }
    const enableGroup = capability === "execution"
      ? editableItems.some((tool) => !tool.execution_enabled)
      : editableItems.some(
          (tool) => tool.execution_enabled && !tool.model_visible,
        );
    const changes = editableItems.map((tool) => ({
      tool_id: tool.tool_id,
      execution_enabled: capability === "execution"
        ? enableGroup
        : tool.execution_enabled,
      model_visible: capability === "execution"
        ? (enableGroup ? tool.model_visible : false)
        : (tool.execution_enabled && enableGroup),
    }));
    const requestScopeKey = pollScopeKey;
    void saveChanges(changes, t("composer.tools.groupSaved", group.name)).catch(() => {
      if (mountedRef.current && pollScopeKeyRef.current === requestScopeKey) onStatus(t("composer.tools.groupSaveFailed"));
    });
  };

  const runTest = (tool: ToolCatalogItem) => {
    setSelectedProviders([]);
    setRepetitions(0);
    setTestCostAcknowledged(false);
    setTestDialog(tool);
  };

  const confirmTest = () => {
    const tool = testDialog;
    if (!tool || selectedProviders.length === 0 || repetitions < 1 || !testCostAcknowledged) return;
    const requestScopeKey = pollScopeKey;
    setError(null);
    setTestingTools((current) => new Set(current).add(tool.tool_id));
    setTestDialog(null);
    void startToolTest(apiPort, tool.tool_id, agentId, workspaceId, selectedProviders, repetitions)
      .then((run) => {
        if (!mountedRef.current || !observeRunsRef.current || pollScopeKeyRef.current !== requestScopeKey) return;
        const nextRuns = new Map(runsRef.current).set(tool.tool_id, run);
        runsRef.current = nextRuns;
        setRuns(nextRuns);
        if (run.status === "completed" || run.status === "failed") {
          setTestingTools((current) => {
            const next = new Set(current);
            next.delete(tool.tool_id);
            return next;
          });
        }
        onStatus(t("composer.tools.testStarted", tool.name));
      })
      .catch((testError: unknown) => {
        if (!mountedRef.current || !observeRunsRef.current || pollScopeKeyRef.current !== requestScopeKey) return;
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
  const availableProviders = providers.filter((provider) => provider.available && !provider.configuration_error?.trim());

  const menu = (
        <section className={`composer-tool-menu${inline ? " composer-tool-menu-inline" : ""}`} aria-label={t("composer.tools.menu")}>
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
              {!inline ? <button
                type="button"
                className="composer-tool-refresh"
                title={t("composer.tools.refresh")}
                aria-label={t("composer.tools.refresh")}
                disabled={loading}
                onClick={() => void load()}
              >
                <span className="codicon codicon-refresh" aria-hidden="true" />
              </button> : null}
              {!inline ? <button
                type="button"
                className="composer-tool-close"
                title={t("composer.tools.close")}
                aria-label={t("composer.tools.close")}
                onClick={() => setOpen(false)}
              >
                <span className="codicon codicon-close" aria-hidden="true" />
              </button> : null}
            </div>
          </header>
          {error ? <div className="composer-tool-error">{error}</div> : null}
          <ComposerToolTree
            groups={groups}
            loading={loading}
            savingToolIds={savingToolIds}
            runs={runs}
            testingTools={testingTools}
            canRunTests={availableProviders.length > 0}
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
          {testDialog ? <div className="composer-tool-test-confirm" role="dialog" aria-modal="true" aria-label={t("composer.tools.test")}
            onClick={(event) => { if (event.target === event.currentTarget) setTestDialog(null); }}>
            <section>
              <strong>{t("composer.tools.testNamed", testDialog.name)}</strong>
              <p>{t("composer.tools.testCostNotice")}</p>
              {availableProviders.map((provider) => <label key={provider.provider_id}>
                <input type="checkbox" checked={selectedProviders.includes(provider.provider_id)}
                  onChange={(event) => setSelectedProviders((current) => event.target.checked
                    ? [...current, provider.provider_id] : current.filter((id) => id !== provider.provider_id))} />
                {provider.provider_id} · {provider.model}
              </label>)}
              <label>{t("composer.tools.testRepetitions")}
                <input type="number" min={1} max={20} value={repetitions || ""} onChange={(event) => setRepetitions(Math.min(20, Number(event.target.value) || 0))} />
              </label>
              <label><input type="checkbox" checked={testCostAcknowledged} onChange={(event) => setTestCostAcknowledged(event.target.checked)} />{t("composer.tools.testCostAcknowledge")}</label>
              <button type="button" onClick={() => setTestDialog(null)}>{t("composer.tools.close")}</button>
              <button type="button" disabled={selectedProviders.length === 0 || repetitions < 1 || !testCostAcknowledged} onClick={confirmTest}>{t("composer.tools.testConfirm")}</button>
            </section>
          </div> : null}
        </section>
  );
  return (
    <div className={inline ? "composer-tool-control composer-tool-control-inline" : "composer-tool-control"} ref={controlRef}>
      {!inline ? <button
        ref={buttonRef}
        type="button"
        className={`composer-icon-button composer-tool-button${open ? " active" : ""}`}
        title={t("composer.tools.select")}
        aria-label={t("composer.tools.select")}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      ><span className="codicon codicon-settings" aria-hidden="true" /></button> : null}
      {inline ? menu : <AnchoredOverlay open={open} anchorRef={buttonRef} placement="top-end" onClose={() => setOpen(false)}>{menu}</AnchoredOverlay>}
    </div>
  );
}
