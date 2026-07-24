import { useState } from "react";
import type { SessionResourceExplorerController } from "../../hooks/useSessionResourceExplorer";
import type {
  GatewayWorkspace,
  GeneratorSessionStrategyMode,
} from "../../types/backend";
import WarmActionDialog from "../WarmActionDialog";

export default function SessionGeneratorManager({
  explorer,
  workspaces,
  activeWorkspaceId,
  currentSessionId,
  onStatusChange,
}: {
  explorer: SessionResourceExplorerController;
  workspaces: GatewayWorkspace[];
  activeWorkspaceId: string | null;
  currentSessionId: string;
  onStatusChange: (message: string) => void;
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

  const sessionFolderChoices = [...explorer.branches.entries()]
    .filter(([key]) => key.startsWith(`${targetWorkspaceId}:`))
    .flatMap(([_key, branch]) => branch.items)
    .filter((node) => node.kind === "folder" && node.folder_id)
    .filter((node, index, items) => items.findIndex(
      (candidate) => candidate.folder_id === node.folder_id,
    ) === index);

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
    await explorer.createGenerator({
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
    onStatusChange(`已创建会话生成器：${name.trim()}`);
  };

  return (
    <>
    <details className="session-generator-manager" data-testid="session-generator-manager">
      <summary>自动生成会话 <span>{explorer.generators?.items.length ?? 0}</span></summary>
      {explorer.generatorError ? <div className="session-resource-error" role="alert">{explorer.generatorError}</div> : null}
      <div className="session-generator-list">
        {explorer.generators?.items.map((generator) => {
          const runs = explorer.generationRuns.get(generator.generator_id);
          return (
            <div className="session-generator-row" key={generator.generator_id}>
              <span className="session-generator-summary">
                <strong>{generator.name}</strong>
                <small>{generator.session_strategy.mode} · {generator.status}</small>
                {generator.status_reason ? <small className="session-resource-error">{generator.status_reason}</small> : null}
              </span>
              <span className="session-generator-actions">
                <button
                  type="button"
                  disabled={generator.status !== "ready"}
                  onClick={() => {
                    void explorer.runGenerator(generator.generator_id)
                      .then((run) => onStatusChange(`生成任务 ${generator.name}: ${run.status}`))
                      .catch((error) => reportError("运行生成器失败", error));
                  }}
                >运行</button>
                <button
                  type="button"
                  onClick={() => void explorer.refreshGenerationRuns(generator.generator_id)
                    .catch((error) => reportError("读取运行历史失败", error))}
                >历史</button>
                {generator.status === "blocked" ? (
                  <button
                    type="button"
                    onClick={() => void explorer.updateGenerator(
                      generator.generator_id,
                      { enabled: true },
                    ).then(() => onStatusChange(`已重新校验生成器：${generator.name}`))
                      .catch((error) => reportError("重新挂载生成器失败", error))}
                  >重试挂载</button>
                ) : null}
                <button
                  type="button"
                  aria-label={`删除生成器 ${generator.name}`}
                  onClick={() => {
                    setDeletingGenerator({
                      generatorId: generator.generator_id,
                      name: generator.name,
                    });
                  }}
                >删除</button>
              </span>
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
          void submit().catch((error) => reportError("创建生成器失败", error));
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
              void explorer.previewGenerator({
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
      ) : <button type="button" className="session-generator-create" onClick={() => {
        resetDraft();
        setCreating(true);
      }}>新建生成器</button>}
    </details>
    <WarmActionDialog
      open={deletingGenerator !== null}
      title="删除会话生成器"
      description={deletingGenerator
        ? `删除生成器“${deletingGenerator.name}”？已有运行和生成会话不会删除。`
        : undefined}
      confirmText="删除"
      danger
      onClose={() => setDeletingGenerator(null)}
      onConfirm={async () => {
        if (!deletingGenerator) {
          throw new Error("生成器删除目标已失效");
        }
        await explorer.deleteGenerator(deletingGenerator.generatorId);
        onStatusChange(`已删除生成器：${deletingGenerator.name}`);
      }}
    />
    </>
  );
}
