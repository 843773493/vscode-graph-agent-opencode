import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
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

const TOOL_GROUP_KIND_ORDER: Record<ToolKind, number> = {
  default: 0,
  collaboration: 1,
  extension: 2,
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
  const menuRef = useRef<HTMLElement | null>(null);
  const [menuPosition, setMenuPosition] = useState({ left: 8, bottom: 8, width: 460 });

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
    if (!open) {
      return;
    }
    const handlePointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node) {
        if (controlRef.current?.contains(event.target) || menuRef.current?.contains(event.target)) {
          return;
        }
      }
      setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  const updateMenuPosition = useCallback(() => {
    const button = buttonRef.current;
    if (!button) {
      return;
    }
    const rect = button.getBoundingClientRect();
    const viewportPadding = 8;
    const width = Math.min(460, window.innerWidth - viewportPadding * 2);
    const left = Math.min(
      Math.max(viewportPadding, rect.right - width),
      window.innerWidth - width - viewportPadding,
    );
    setMenuPosition({
      left,
      bottom: window.innerHeight - rect.top + 8,
      width,
    });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    updateMenuPosition();
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [open, updateMenuPosition]);

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
    const changedIds = new Set(changes.map((change) => change.tool_id));
    const enabledById = new Map(
      changes.map((change) => [change.tool_id, change.enabled]),
    );
    setSavingToolIds((current) => new Set([...current, ...changedIds]));
    setTools((current) => current.map((tool) => {
      const enabled = enabledById.get(tool.tool_id);
      return enabled === undefined ? tool : { ...tool, enabled };
    }));
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
      const refreshedTools = await getToolCatalog(apiPort, agentId, workspaceId);
      const refreshedById = new Map(
        refreshedTools
          .filter((tool) => changedIds.has(tool.tool_id))
          .map((tool) => [tool.tool_id, tool]),
      );
      setTools((current) => current.map(
        (tool) => refreshedById.get(tool.tool_id) ?? tool,
      ));
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

  const toggleTool = (toolId: string) => {
    const tool = tools.find((item) => item.tool_id === toolId);
    if (!tool || savingToolIds.has(toolId)) {
      return;
    }
    void saveChanges(
      [{ tool_id: toolId, enabled: !tool.enabled }],
      `${tool.name} 工具设置已保存`,
    ).catch(() => {
      onStatus("工具设置保存失败");
    });
  };

  const toggleGroup = (group: ToolGroup) => {
    if (group.items.some((tool) => savingToolIds.has(tool.tool_id))) {
      return;
    }
    const enableGroup = group.items.some((tool) => !tool.enabled);
    const changes = group.items.map((tool) => ({
      tool_id: tool.tool_id,
      enabled: enableGroup,
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

  const enabledCount = tools.filter((tool) => tool.enabled).length;

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
      {open && typeof document !== "undefined" ? createPortal(
        <section
          ref={menuRef}
          className="composer-tool-menu"
          aria-label="工具选择与测试"
          style={menuPosition}
        >
          <header className="composer-tool-menu-header">
            <div>
              <strong>工具</strong>
              <span>{loading ? "正在读取…" : `${enabledCount}/${tools.length} 已开启`}</span>
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
            onToggleGroup={toggleGroup}
            onToggleTool={toggleTool}
            onRunTest={runTest}
          />
        </section>,
        document.body,
      ) : null}
    </div>
  );
}
