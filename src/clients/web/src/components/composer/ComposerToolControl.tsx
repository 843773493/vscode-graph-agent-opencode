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
import AnchoredOverlay from "../AnchoredOverlay";
import {
  applyToolSelectionChanges,
  restoreToolSelectionAfterSaveFailure,
} from "./toolSelectionState";

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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [catalog, history] = await Promise.all([
        getToolCatalog(apiPort, agentId, workspaceId),
        listToolTestRuns(apiPort, workspaceId),
      ]);
      setTools(catalog);
      setRuns(latestRunsByTool(history));
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : String(loadError);
      setError(message);
      throw loadError;
    } finally {
      setLoading(false);
    }
  }, [agentId, apiPort, workspaceId]);

  useEffect(() => {
    if (!open) {
      return;
    }
    void load().catch(() => {
      onStatus("工具列表加载失败");
    });
  }, [load, onStatus, open]);

  useEffect(() => {
    if (testingTools.size === 0) {
      return;
    }
    const timer = window.setInterval(() => {
      for (const toolName of testingTools) {
        const run = runs.get(toolName);
        if (!run) {
          continue;
        }
        void getToolTestRun(apiPort, run.run_id, workspaceId)
          .then((nextRun) => {
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
            const message = pollError instanceof Error ? pollError.message : String(pollError);
            setError(`测试进度读取失败：${message}`);
          });
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [apiPort, runs, testingTools, workspaceId]);

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
      const message = saveError instanceof Error ? saveError.message : String(saveError);
      setError(`工具设置保存失败：${message}`);
      try {
        const refreshedTools = await getToolCatalog(apiPort, agentId, workspaceId);
        setTools(restoreToolSelectionAfterSaveFailure(previousTools, refreshedTools));
      } catch (refreshError) {
        setTools(restoreToolSelectionAfterSaveFailure(previousTools, null));
        const refreshMessage = refreshError instanceof Error
          ? refreshError.message
          : String(refreshError);
        setError(`工具设置保存失败，且状态刷新失败：${message}；${refreshMessage}`);
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
      `${tool.name} 工具设置已保存`,
    ).catch(() => {
      onStatus("工具设置保存失败");
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
      `${tool.name} 的模型可见性已保存`,
    ).catch(() => {
      onStatus("工具设置保存失败");
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
    void saveChanges(changes, `${group.name} 工具组设置已保存`).catch(() => {
      onStatus("工具组设置保存失败");
    });
  };

  const runTest = (tool: ToolCatalogItem) => {
    setError(null);
    setTestingTools((current) => new Set(current).add(tool.tool_id));
    void startToolTest(apiPort, tool.tool_id, agentId, workspaceId)
      .then((run) => {
        setRuns((current) => new Map(current).set(tool.tool_id, run));
        onStatus(`已启动 ${tool.name} 模型工具测试`);
      })
      .catch((testError: unknown) => {
        const message = testError instanceof Error ? testError.message : String(testError);
        setError(`测试启动失败：${message}`);
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
        title="选择和测试工具"
        aria-label="选择和测试工具"
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
          aria-label="工具选择与测试"
        >
          <header className="composer-tool-menu-header">
            <div>
              <strong>工具</strong>
              <span>
                {loading
                  ? "正在读取…"
                  : `${executionCount}/${tools.length} 可调用 · ${modelVisibleCount}/${tools.length} 模型可见`}
              </span>
            </div>
            <div className="composer-tool-menu-header-actions">
              <button
                type="button"
                className="composer-tool-refresh"
                title="刷新工具列表"
                aria-label="刷新工具列表"
                disabled={loading}
                onClick={() => void load()}
              >
                <span className="codicon codicon-refresh" aria-hidden="true" />
              </button>
              <button
                type="button"
                className="composer-tool-close"
                title="关闭工具面板"
                aria-label="关闭工具面板"
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
