import { useEffect, useState } from "react";
import { listSessionCatalogChildren } from "../../api";
import type { SessionGeneratorResourcesController } from "../../hooks/sessionResourceExplorer/useSessionGeneratorResources";
import type {
  GatewayWorkspace,
  GeneratorSessionStrategyMode,
} from "../../types/backend";
import WarmActionDialog from "../WarmActionDialog";
import {
  generatorStatusPresentation,
  generatorStrategyLabel,
  generatorTriggerLabel,
} from "./sessionGeneratorPresentation";

export default function SessionGeneratorManager({
  apiPort,
  generatorResources,
  workspaces,
  activeWorkspaceId,
  currentSessionId,
  onStatusChange,
  onOpenConnectionManager,
  onReconnectWorkspace,
  onStartWorkspace,
}: {
  apiPort: number;
  generatorResources: SessionGeneratorResourcesController;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  currentSessionId: string;
  onStatusChange: (message: string) => void;
  onOpenConnectionManager: () => void;
  onReconnectWorkspace: (workspaceId: string) => Promise<void>;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("定时会话任务");
  const [prompt, setPrompt] = useState("");
  const [strategy, setStrategy] = useState<GeneratorSessionStrategyMode>("new_per_run");
  const [targetWorkspaceId, setTargetWorkspaceId] = useState(activeWorkspaceId ?? "");
  const [targetSessionId, setTargetSessionId] = useState(currentSessionId);
  const [placementKind, setPlacementKind] = useState<"workspace" | "session" | "session_folder">("workspace");
  const [targetFolderId, setTargetFolderId] = useState("");
  const [titleTemplate, setTitleTemplate] = useState("{generator.name} {generated_at:yyyy-MM-dd HH-mm-ss}");
  const [pathTemplate, setPathTemplate] = useState("{generator.name}/{generated_at:yyyy-MM-dd}");
  const [triggerType, setTriggerType] = useState<"manual" | "interval" | "cron">("interval");
  const [intervalSeconds, setIntervalSeconds] = useState("3600");
  const [cronExpression, setCronExpression] = useState("0 * * * *");
  const [previewPath, setPreviewPath] = useState("");
  const [deletingGenerator, setDeletingGenerator] = useState<{
    generatorId: string;
    name: string;
  } | null>(null);
  const [recoveringGeneratorIds, setRecoveringGeneratorIds] =
    useState<Set<string>>(new Set());
  const [sessionFolderChoices, setSessionFolderChoices] = useState<Array<{
    folder_id: string;
    name: string;
  }>>([]);

  const reportError = (prefix: string, error: unknown) => {
    onStatusChange(`${prefix}: ${error instanceof Error ? error.message : String(error)}`);
  };

  const resetDraft = () => {
    setName("定时会话任务");
    setPrompt("");
    setStrategy("new_per_run");
    setTargetWorkspaceId(activeWorkspaceId ?? "");
    setTargetSessionId(currentSessionId);
    setPlacementKind("workspace");
    setTargetFolderId("");
    setTitleTemplate("{generator.name} {generated_at:yyyy-MM-dd HH-mm-ss}");
    setPathTemplate("{generator.name}/{generated_at:yyyy-MM-dd}");
    setTriggerType("interval");
    setIntervalSeconds("3600");
    setCronExpression("0 * * * *");
    setPreviewPath("");
  };

  const closeDraft = () => {
    setCreating(false);
    resetDraft();
  };

  const recoverGenerator = async (
    generatorId: string,
    generatorName: string,
    targetWorkspace: GatewayWorkspace | undefined,
  ) => {
    setRecoveringGeneratorIds((previous) => new Set(previous).add(generatorId));
    try {
      if (targetWorkspace?.connection_kind === "remote_gateway") {
        await onReconnectWorkspace(targetWorkspace.workspace_id);
      } else if (targetWorkspace?.status === "offline" && targetWorkspace.managed) {
        await onStartWorkspace(targetWorkspace.workspace_id);
      }
      await generatorResources.updateGenerator(generatorId, { enabled: true });
      onStatusChange(`已重新校验自动化：${generatorName}`);
    } catch (error) {
      reportError("恢复生成器失败", error);
    } finally {
      setRecoveringGeneratorIds((previous) => {
        const next = new Set(previous);
        next.delete(generatorId);
        return next;
      });
    }
  };

  useEffect(() => {
    if (!creating || !targetWorkspaceId) {
      setSessionFolderChoices([]);
      return;
    }
    let cancelled = false;
    void listSessionCatalogChildren(apiPort, targetWorkspaceId)
      .then((page) => {
        if (cancelled) return;
        setSessionFolderChoices(page.items.flatMap((node) => (
          node.kind === "folder" && node.folder_id
            ? [{ folder_id: node.folder_id, name: node.name }]
            : []
        )));
      })
      .catch(() => {
        if (!cancelled) setSessionFolderChoices([]);
      });
    return () => {
      cancelled = true;
    };
  }, [apiPort, creating, targetWorkspaceId]);

  const submit = async () => {
    const workspaceId = targetWorkspaceId || activeWorkspaceId;
    if (!workspaceId) {
      throw new Error("请选择生成会话所在的工作区");
    }
    const target = strategy === "new_per_run"
      ? null
      : { workspace_id: workspaceId, session_id: targetSessionId.trim() };
    if (strategy !== "new_per_run" && !target?.session_id) {
      throw new Error("继续/分支策略必须填写目标会话 ID");
    }
    if (placementKind === "session" && !targetSessionId.trim()) {
      throw new Error("挂载到会话时必须填写目标会话 ID");
    }
    if (placementKind === "session_folder" && !targetFolderId) {
      throw new Error("挂载到会话文件夹时必须选择目标文件夹");
    }
    await generatorResources.createGenerator({
      name: name.trim(),
      generator_type: { type_id: "builtin.agent_prompt", version: "1" },
      enabled: true,
      trigger: {
        type: triggerType,
        timezone: "UTC",
        interval_seconds: triggerType === "interval" ? Number(intervalSeconds) : null,
        expression: triggerType === "cron" ? cronExpression.trim() : null,
      },
      placement: {
        kind: placementKind,
        workspace_id: workspaceId,
        session_id: placementKind === "session" ? targetSessionId.trim() : null,
        folder_id: placementKind === "session_folder" ? targetFolderId : null,
      },
      execution_workspace_id: workspaceId,
      context_source: { kind: "fresh" },
      created_from: currentSessionId && activeWorkspaceId
        ? { workspace_id: activeWorkspaceId, session_id: currentSessionId }
        : null,
      naming: {
        title_template: titleTemplate,
        path_template: pathTemplate.split("/").map((segment) => segment.trim()).filter(Boolean),
      },
      session_strategy: {
        mode: strategy,
        target,
        concurrency: "queue",
        report_back: strategy === "fork_new_and_report_back" ? "continue_agent" : "none",
      },
      policies: {
        overlap: "allow",
        misfire: "run_latest",
        mount_missing: "pause",
        delete_outputs: "keep",
      },
      ui_policy: { on_run_started: "stay", on_run_completed: "notify" },
      config: { prompt: prompt.trim(), session_title: name.trim() },
    });
    closeDraft();
    onStatusChange(`已创建会话自动化：${name.trim()}`);
  };

  return (
    <>
    <section className="session-generator-manager" data-testid="session-generator-manager" aria-label="会话自动化">
      <header className="session-automation-header">
        <div>
          <h2>会话自动化</h2>
          <p>按计划创建、继续或分支会话。</p>
        </div>
        {!creating ? <button type="button" className="session-generator-create" onClick={() => {
          resetDraft();
          setCreating(true);
        }}>新建自动化</button> : null}
      </header>
      <div className="session-automation-summary" aria-label="自动化状态摘要">
        <span>{generatorResources.generators?.items.length ?? 0} 个自动化</span>
        <span>{generatorResources.generators?.items.filter((item) => item.status === "ready").length ?? 0} 个正常</span>
        <span className={generatorResources.generators?.items.some((item) => item.status === "blocked") ? "attention" : ""}>
          {generatorResources.generators?.items.filter((item) => item.status === "blocked").length ?? 0} 个需要处理
        </span>
      </div>
      {generatorResources.generatorError ? <div className="session-resource-error" role="alert">{generatorResources.generatorError}</div> : null}
      <div className="session-generator-list">
        {generatorResources.generators?.items.map((generator) => {
          const runs = generatorResources.generationRuns.get(generator.generator_id);
          const targetWorkspace = workspaces.find(
            (workspace) => workspace.workspace_id === generator.placement.workspace_id,
          );
          const status = generatorStatusPresentation(generator, targetWorkspace);
          const recovering = recoveringGeneratorIds.has(generator.generator_id);
          return (
            <div className="session-generator-row" key={generator.generator_id}>
              <div className="session-generator-header">
                <strong title={generator.name}>{generator.name}</strong>
                <span className={`session-generator-status ${status.tone}`}>{status.label}</span>
              </div>
              <div className="session-generator-meta">
                <span>{generatorStrategyLabel(generator.session_strategy.mode)}</span>
                {targetWorkspace ? <span title={targetWorkspace.root_path}>目标：{targetWorkspace.name}</span> : null}
                <span>{generatorTriggerLabel(generator.trigger)}</span>
              </div>
              {status.title && status.message ? (
                <div className={`session-generator-notice ${status.tone}`} role={status.tone === "blocked" ? "alert" : "status"}>
                  <strong>{status.title}</strong>
                  <span>{status.message}</span>
                  {status.technicalDetail ? (
                    <details>
                      <summary>查看技术详情</summary>
                      <code>{status.technicalDetail}</code>
                    </details>
                  ) : null}
                </div>
              ) : null}
              <div className="session-generator-actions">
                <button
                  type="button"
                  className="primary"
                  disabled={generator.status !== "ready"}
                  title={generator.status === "ready" ? "立即运行自动化" : "请先恢复生成目标"}
                  onClick={() => {
                    void generatorResources.runGenerator(generator.generator_id)
                      .then((run) => onStatusChange(`生成任务 ${generator.name}: ${run.status}`))
                      .catch((error) => reportError("运行生成器失败", error));
                  }}
                >运行</button>
                <button
                  type="button"
                  onClick={() => void generatorResources.refreshGenerationRuns(generator.generator_id)
                    .catch((error) => reportError("读取运行历史失败", error))}
                >历史</button>
                {generator.status === "blocked" && targetWorkspace ? (
                  <button
                    type="button"
                    className="recovery"
                    disabled={recovering}
                    onClick={() => void recoverGenerator(
                      generator.generator_id,
                      generator.name,
                      targetWorkspace,
                    )}
                  >
                    {recovering
                      ? "正在恢复…"
                      : targetWorkspace?.connection_kind === "remote_gateway"
                        ? "重新连接"
                        : targetWorkspace?.status === "offline" && targetWorkspace.managed
                          ? "启动工作区"
                          : "重新校验"}
                  </button>
                ) : null}
                {generator.status === "blocked" && !targetWorkspace ? (
                  <button type="button" className="recovery" onClick={onOpenConnectionManager}>处理问题</button>
                ) : null}
                {generator.status === "blocked" && targetWorkspace?.connection_kind === "remote_gateway" ? (
                  <button type="button" onClick={onOpenConnectionManager}>连接管理</button>
                ) : null}
                <details className="session-generator-more">
                  <summary aria-label={`更多生成器操作：${generator.name}`} title="更多操作">•••</summary>
                  <div role="menu">
                    <button
                      type="button"
                      className="danger"
                      role="menuitem"
                      onClick={() => {
                        setDeletingGenerator({
                          generatorId: generator.generator_id,
                          name: generator.name,
                        });
                      }}
                    >删除自动化</button>
                  </div>
                </details>
              </div>
              {runs ? (
                <div className="session-generator-runs" aria-label={`${generator.name} 运行历史`}>
                  {runs.length === 0 ? <small>暂无运行记录</small> : runs.slice(0, 5).map((run) => (
                    <div className={`session-generator-run ${run.status}`} key={run.run_id}>
                      <span><strong>{run.status}</strong> · {run.run_id}</span>
                      {run.outputs.map((output) => (
                        <small key={`${output.workspace_id}:${output.session_id}`}>
                          {output.title || output.session_id} · {output.storage_relative_path || output.navigation_path.join("/") || "工作区根"}
                        </small>
                      ))}
                      {run.error ? <small className="session-resource-error">{run.error}</small> : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      {creating ? (
        <form className="session-generator-form" onSubmit={(event) => {
          event.preventDefault();
          void submit().catch((error) => reportError("创建自动化失败", error));
        }}>
          <label>名称<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>提示词<textarea required value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="每次触发时让 Agent 执行什么？" /></label>
          <label>会话策略
            <select value={strategy} onChange={(event) => setStrategy(event.target.value as GeneratorSessionStrategyMode)}>
              <option value="new_per_run">每次创建新会话</option>
              <option value="continue_existing">继续指定会话</option>
              <option value="fork_new_and_report_back">/new 分支后继续旧会话</option>
            </select>
          </label>
          <label>触发方式
            <select value={triggerType} onChange={(event) => setTriggerType(event.target.value as "manual" | "interval" | "cron")}>
              <option value="interval">固定间隔</option>
              <option value="cron">Cron</option>
              <option value="manual">仅手动</option>
            </select>
          </label>
          {triggerType === "interval" ? <label>间隔秒数<input type="number" min="1" required value={intervalSeconds} onChange={(event) => setIntervalSeconds(event.target.value)} /></label> : null}
          {triggerType === "cron" ? <label>Cron（UTC）<input required value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} /></label> : null}
          <label>工作区
            <select value={targetWorkspaceId} onChange={(event) => setTargetWorkspaceId(event.target.value)}>
              {workspaces.map((workspace) => (
                <option key={workspace.workspace_id} value={workspace.workspace_id}>
                  {workspace.name} — {workspace.root_path} · {workspace.status} · {workspace.workspace_id.slice(-8)}
                </option>
              ))}
            </select>
          </label>
          <label>生成器挂载位置
            <select value={placementKind} onChange={(event) => setPlacementKind(event.target.value as "workspace" | "session" | "session_folder")}>
              <option value="workspace">工作区根</option>
              <option value="session">某个会话</option>
              <option value="session_folder">某个会话文件夹</option>
            </select>
          </label>
          {strategy !== "new_per_run" || placementKind === "session" ? <label>目标会话 ID<input required value={targetSessionId} onChange={(event) => setTargetSessionId(event.target.value)} /></label> : null}
          {placementKind === "session_folder" ? (
            <label>目标会话文件夹
              <input
                required
                list="session-generator-folder-options"
                value={targetFolderId}
                onChange={(event) => setTargetFolderId(event.target.value.trim())}
                placeholder="输入任意文件夹 ID，或选择已加载文件夹"
              />
              <datalist id="session-generator-folder-options">
                {sessionFolderChoices.map((folder) => (
                  <option key={folder.folder_id} value={folder.folder_id ?? ""}>{folder.name} [{folder.folder_id}]</option>
                ))}
              </datalist>
            </label>
          ) : null}
          <label>会话名称模板<input required value={titleTemplate} onChange={(event) => setTitleTemplate(event.target.value)} /></label>
          <label>目录模板（留空表示挂载文件夹）<input value={pathTemplate} onChange={(event) => setPathTemplate(event.target.value)} /></label>
          <div className="session-generator-preview">
            <button type="button" onClick={() => {
              void generatorResources.previewGenerator({
                name: name.trim(),
                naming: {
                  title_template: titleTemplate,
                  path_template: pathTemplate.split("/").map((segment) => segment.trim()).filter(Boolean),
                },
                session_title: name.trim(),
                placement: {
                  kind: placementKind,
                  workspace_id: targetWorkspaceId || activeWorkspaceId,
                  session_id: placementKind === "session" ? targetSessionId.trim() : null,
                  folder_id: placementKind === "session_folder" ? targetFolderId : null,
                },
                session_strategy: {
                  mode: strategy,
                  target: strategy === "new_per_run"
                    ? null
                    : {
                      workspace_id: targetWorkspaceId || activeWorkspaceId,
                      session_id: targetSessionId.trim(),
                    },
                  concurrency: "queue",
                  report_back: strategy === "fork_new_and_report_back"
                    ? "continue_agent"
                    : "none",
                },
              }).then((preview) => setPreviewPath(preview.relative_path)).catch((error) => reportError("预览命名路径失败", error));
            }}>预览物理路径模板（稳定 ID 在运行时分配）</button>
            {previewPath ? <code>{previewPath}</code> : null}
          </div>
          <div className="session-generator-form-actions">
            <button type="button" onClick={closeDraft}>取消</button>
            <button type="submit">创建</button>
          </div>
        </form>
      ) : null}
    </section>
    <WarmActionDialog
      open={deletingGenerator !== null}
      title="删除会话自动化"
      description={deletingGenerator
        ? `删除自动化“${deletingGenerator.name}”？已有运行和生成会话不会删除。`
        : undefined}
      confirmText="删除"
      danger
      onClose={() => setDeletingGenerator(null)}
      onConfirm={async () => {
        if (!deletingGenerator) {
          throw new Error("自动化删除目标已失效");
        }
        await generatorResources.deleteGenerator(deletingGenerator.generatorId);
        onStatusChange(`已删除自动化：${deletingGenerator.name}`);
      }}
    />
    </>
  );
}
