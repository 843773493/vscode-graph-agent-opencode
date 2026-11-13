import { t } from "../../../../shared/core/i18n/index.js";
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
  if (run.status === "queued") return t("composer.tools.testQueued");
  if (run.status === "running") {
    return t("composer.tools.progress", run.progress, passed, completed);
  }
  if (completed === 0) return run.status === "failed" ? t("composer.tools.testFailed") : t("composer.tools.noResults");
  return t("composer.tools.successRate", Math.round((passed / completed) * 100), passed, completed);
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
  canRunTests,
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
  canRunTests: boolean;
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
                title={collapsed ? t("composer.tools.expandGroup") : t("composer.tools.collapseGroup")}
                aria-label={collapsed ? t("composer.tools.expandNamedGroup", group.name) : t("composer.tools.collapseNamedGroup", group.name)}
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
                    aria-label={t(executionState === "on" ? "composer.tools.disableExecution" : "composer.tools.enableExecution", group.name)}
                    title={t(executionState === "on" ? "composer.tools.disableGroupHint" : "composer.tools.enableGroupHint")}
                    disabled={groupSaving || group.items.every((tool) => tool.execution_locked)}
                    onClick={() => onToggleGroupCapability(group, "execution")}
                  >
                    <span className="codicon codicon-tools" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className="composer-tool-capability-button"
                    data-state={modelState}
                    aria-pressed={ariaPressed(modelState)}
                    aria-label={t(modelState === "on" ? "composer.tools.hideDefinition" : "composer.tools.showDefinition", group.name)}
                    title={t(modelState === "on" ? "composer.tools.hideDefinitionHint" : "composer.tools.showDefinitionHint")}
                    disabled={groupSaving || executionState === "off" || group.items.every((tool) => tool.model_visibility_locked)}
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
                  const availabilityLabel = tool.availability === "available" ? t("composer.tools.sourceAvailable")
                    : tool.availability === "unavailable" ? t("composer.tools.sourceUnavailable")
                      : t("composer.tools.sourceUnknown");
                  return (
                    <div className="composer-tool-item" key={tool.tool_id}>
                      <div className="composer-tool-item-main">
                        <div className="composer-tool-item-capability-buttons">
                          <button
                            type="button"
                            className="composer-tool-capability-button"
                            data-state={tool.execution_enabled ? "on" : "off"}
                            aria-pressed={tool.execution_enabled}
                            aria-label={t(tool.execution_enabled ? "composer.tools.disableExecution" : "composer.tools.enableExecution", tool.name)}
                            title={t(tool.execution_enabled ? "composer.tools.disableHint" : "composer.tools.enableHint")}
                            disabled={saving || tool.execution_locked}
                            onClick={() => onToggleToolExecution(tool.tool_id)}
                          >
                            <span className="codicon codicon-tools" aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            className="composer-tool-capability-button"
                            data-state={tool.model_visible ? "on" : "off"}
                            aria-pressed={tool.model_visible}
                            aria-label={t(tool.model_visible ? "composer.tools.hideDefinition" : "composer.tools.showDefinition", tool.name)}
                            title={t(tool.model_visible ? "composer.tools.hideDefinitionHint" : "composer.tools.showDefinitionHint")}
                            disabled={saving || tool.model_visibility_locked || !tool.execution_enabled}
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
                              title={saving ? t("composer.tools.saving", tool.name) : undefined}
                              aria-label={saving ? t("composer.tools.saving", tool.name) : undefined}
                            >
                              {saving ? (
                                <span
                                  className="codicon codicon-loading codicon-modifier-spin"
                                  aria-hidden="true"
                                />
                              ) : null}
                            </span>
                          </span>
                          <span>{tool.description || t("composer.tools.backendTool")}</span>
                          {tool.origin !== "builtin" ? <span>{t("composer.tools.sourceOrigin", tool.origin === "mcp" ? t("composer.tools.origin.mcp") : t("composer.tools.origin.custom"))} · {availabilityLabel}{tool.availability_detail ? " · " + tool.availability_detail : ""}</span> : null}
                          {tool.execution_locked ? <span>{t("composer.tools.policyExecutionLocked")}</span> : null}
                          {tool.model_visibility_locked ? <span>{t("composer.tools.policyVisibilityLocked")}</span> : null}
                          <details><summary>{t("composer.tools.parameters")}</summary><pre>{JSON.stringify(tool.parameters, null, 2)}</pre></details>
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
                          disabled={!tool.test_supported || running || !canRunTests}
                          title={
                            tool.test_supported
                              ? t("composer.tools.testNamed", tool.name)
                              : t("composer.tools.noTests")
                          }
                          onClick={() => onRunTest(tool)}
                        >
                          <span
                            className={`codicon codicon-${running ? "loading codicon-modifier-spin" : "beaker"}`}
                            aria-hidden="true"
                          />
                          <span>{t("composer.tools.test")}</span>
                        </button>
                      </div>
                      {run && run.providers.length > 0 ? (
                        <div className="composer-tool-provider-results">
                          {run.providers.map((provider) => (
                            <span
                              key={provider.provider_id}
                              title={
                                t("composer.tools.providerSummary", provider.passed, provider.failed, provider.model_calls, provider.reasoning_only_calls, provider.transient_retries)
                              }
                            >
                              {provider.provider_id} · {provider.model} · {provider.success_rate}%
                              {t("composer.tools.modelCalls", provider.model_calls)}
                              {provider.reasoning_only_calls > 0
                                ? t("composer.tools.reasoningCalls", provider.reasoning_only_calls)
                                : null}
                              {provider.transient_retries > 0
                                ? t("composer.tools.retries", provider.transient_retries)
                                : null}
                            </span>
                          ))}
                        </div>
                      ) : null}
                      {failedAttempts.length > 0 ? (
                        <details className="composer-tool-attempt-errors">
                          <summary>{t("composer.tools.failedDetails", failedAttempts.length)}</summary>
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
        <div className="composer-tool-empty">{t("composer.tools.empty")}</div>
      ) : null}
    </div>
  );
}
