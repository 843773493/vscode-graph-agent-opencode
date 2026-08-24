import React from "react";
import type {
  ToolCatalogItem,
  ToolKind,
  ToolTestRun,
} from "../../types/toolTesting";

export interface ToolGroup {
  id: string;
  name: string;
  kind: ToolKind;
  items: ToolCatalogItem[];
}

function runMetric(run: ToolTestRun): string {
  const completed = run.providers.reduce((sum, item) => sum + item.completed, 0);
  const passed = run.providers.reduce((sum, item) => sum + item.passed, 0);
  if (run.status === "queued") return "等待测试";
  if (run.status === "running") {
    return `进度 ${run.progress}% · ${passed}/${completed} 通过`;
  }
  if (completed === 0) return run.status === "failed" ? "测试失败" : "暂无结果";
  return `成功率 ${Math.round((passed / completed) * 100)}% · ${passed}/${completed} 通过`;
}

function capabilityState(
  values: boolean[],
): "on" | "off" | "mixed" {
  if (values.length === 0 || values.every((value) => !value)) return "off";
  if (values.every(Boolean)) return "on";
  return "mixed";
}

function ariaPressed(
  state: "on" | "off" | "mixed",
): boolean | "mixed" {
  return state === "mixed" ? "mixed" : state === "on";
}

export default function ComposerToolTree({
  groups,
  loading,
  savingToolIds,
  runs,
  testingTools,
  collapsedGroups,
  onToggleCollapsed,
  onToggleGroupCapability,
  onToggleToolExecution,
  onToggleToolModelVisibility,
  onRunTest,
}: {
  groups: ToolGroup[];
  loading: boolean;
  savingToolIds: Set<string>;
  runs: Map<string, ToolTestRun>;
  testingTools: Set<string>;
  collapsedGroups: Set<string>;
  onToggleCollapsed: (groupId: string) => void;
  onToggleGroupCapability: (
    group: ToolGroup,
    capability: "execution" | "model",
  ) => void;
  onToggleToolExecution: (toolId: string) => void;
  onToggleToolModelVisibility: (toolId: string) => void;
  onRunTest: (tool: ToolCatalogItem) => void;
}): React.ReactNode {
  return (
    <div className="composer-tool-tree">
      {groups.map((group) => {
        const collapsed = collapsedGroups.has(group.id);
        const executionState = capabilityState(
          group.items.map((tool) => tool.execution_enabled),
        );
        const modelState = capabilityState(
          group.items.map((tool) => tool.model_visible),
        );
        const groupSaving = group.items.some((tool) =>
          savingToolIds.has(tool.tool_id)
        );
        return (
          <div className="composer-tool-group" key={group.id}>
            <div className="composer-tool-group-row">
              <button
                type="button"
                className="composer-tool-disclosure"
                title={collapsed ? "展开工具组" : "折叠工具组"}
                aria-label={collapsed ? `展开 ${group.name}` : `折叠 ${group.name}`}
                aria-expanded={!collapsed}
                onClick={() => onToggleCollapsed(group.id)}
              >
                <span
                  className={`codicon codicon-chevron-${collapsed ? "right" : "down"}`}
                  aria-hidden="true"
                />
              </button>
              <div className="composer-tool-group-toggle">
                <span>{group.name}</span>
                <small>
                  {group.items.filter((tool) => tool.execution_enabled).length}/
                  {group.items.length}
                </small>
                <div className="composer-tool-capability-buttons">
                  <button
                    type="button"
                    className="composer-tool-capability-button"
                    data-state={executionState}
                    aria-pressed={ariaPressed(executionState)}
                    aria-label={`${group.name}：${executionState === "on" ? "关闭" : "开启"}工具能力`}
                    title={`${executionState === "on" ? "关闭" : "开启"}工具能力：允许 Agent 调用此组工具`}
                    disabled={groupSaving}
                    onClick={() => onToggleGroupCapability(group, "execution")}
                  >
                    <span className="codicon codicon-tools" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="composer-tool-capability-button"
                    data-state={modelState}
                    aria-pressed={ariaPressed(modelState)}
                    aria-label={`${group.name}：${modelState === "on" ? "隐藏" : "显示"}工具说明`}
                    title={`${modelState === "on" ? "隐藏" : "显示"}工具说明：控制模型是否看到工具定义`}
                    disabled={groupSaving || executionState === "off"}
                    onClick={() => onToggleGroupCapability(group, "model")}
                  >
                    <span className="codicon codicon-eye" aria-hidden="true" />
                  </button>
                </div>
              </div>
            </div>
            {!collapsed
              ? group.items.map((tool) => {
                  const run = runs.get(tool.tool_id);
                  const running = testingTools.has(tool.tool_id);
                  const saving = savingToolIds.has(tool.tool_id);
                  const failedAttempts = run?.attempts.filter((attempt) => !attempt.passed) ?? [];
                  return (
                    <div className="composer-tool-item" key={tool.tool_id}>
                      <div className="composer-tool-item-main">
                        <div className="composer-tool-item-capability-buttons">
                          <button
                            type="button"
                            className="composer-tool-capability-button"
                            data-state={tool.execution_enabled ? "on" : "off"}
                            aria-pressed={tool.execution_enabled}
                            aria-label={`${tool.name}：${tool.execution_enabled ? "关闭" : "开启"}工具能力`}
                            title={`${tool.execution_enabled ? "关闭" : "开启"}工具能力：允许 Agent 调用此工具`}
                            disabled={saving}
                            onClick={() => onToggleToolExecution(tool.tool_id)}
                          >
                            <span className="codicon codicon-tools" aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            className="composer-tool-capability-button"
                            data-state={tool.model_visible ? "on" : "off"}
                            aria-pressed={tool.model_visible}
                            aria-label={`${tool.name}：${tool.model_visible ? "隐藏" : "显示"}工具说明`}
                            title={`${tool.model_visible ? "隐藏" : "显示"}工具说明：控制模型是否看到工具定义`}
                            disabled={saving || !tool.execution_enabled}
                            onClick={() => onToggleToolModelVisibility(tool.tool_id)}
                          >
                            <span className="codicon codicon-eye" aria-hidden="true" />
                          </button>
                        </div>
                        <span className="composer-tool-item-copy">
                          <span className="composer-tool-item-name-row">
                            <strong>{tool.name}</strong>
                            <span
                              className="composer-tool-save-status"
                              title={saving ? `正在保存 ${tool.name}` : undefined}
                              aria-label={saving ? `正在保存 ${tool.name}` : undefined}
                            >
                              {saving ? (
                                <span
                                  className="codicon codicon-loading codicon-modifier-spin"
                                  aria-hidden="true"
                                />
                              ) : null}
                            </span>
                          </span>
                          <span>{tool.description || "后端工具"}</span>
                        </span>
                      </div>
                      <div className="composer-tool-test-summary">
                        {run ? (
                          <span className={`tool-test-metric ${run.status}`}>
                            {runMetric(run)}
                          </span>
                        ) : null}
                        <button
                          type="button"
                          className="composer-tool-test-button"
                          disabled={!tool.test_supported || running}
                          title={
                            tool.test_supported
                              ? `测试 ${tool.name}`
                              : "该工具暂未提供测试用例"
                          }
                          onClick={() => onRunTest(tool)}
                        >
                          <span
                            className={`codicon codicon-${running ? "loading codicon-modifier-spin" : "beaker"}`}
                            aria-hidden="true"
                          />
                          <span>测试</span>
                        </button>
                      </div>
                      {run && run.providers.length > 0 ? (
                        <div className="composer-tool-provider-results">
                          {run.providers.map((provider) => (
                            <span
                              key={provider.provider_id}
                              title={
                                `${provider.passed} 通过，${provider.failed} 失败；` +
                                `实际请求模型 ${provider.model_calls} 次，` +
                                `其中 ${provider.reasoning_only_calls} 次只返回 reasoning，` +
                                `${provider.transient_retries} 次瞬态故障重试`
                              }
                            >
                              {provider.provider_id} · {provider.model} · {provider.success_rate}%
                              {` · ${provider.model_calls} 次模型请求`}
                              {provider.reasoning_only_calls > 0
                                ? ` · ${provider.reasoning_only_calls} 次纯 reasoning`
                                : null}
                              {provider.transient_retries > 0
                                ? ` · ${provider.transient_retries} 次网络/服务器重试`
                                : null}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      {failedAttempts.length > 0 ? (
                        <details className="composer-tool-attempt-errors">
                          <summary>{failedAttempts.length} 条失败详情</summary>
                          <ul>
                            {failedAttempts.map((attempt) => (
                              <li key={attempt.attempt_id}>
                                <strong>{attempt.provider_id} · {attempt.case_id}</strong>
                                <span>{attempt.error || attempt.detail}</span>
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : null}
                    </div>
                  );
                })
              : null}
          </div>
        );
      })}
      {!loading && groups.length === 0 ? (
        <div className="composer-tool-empty">后端没有返回可用工具</div>
      ) : null}
    </div>
  );
}
