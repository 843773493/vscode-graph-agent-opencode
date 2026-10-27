import { useState, type FormEvent } from "react";
import type { PersistentTaskConfigurationDTO } from "../../../api/persistentTasks";

const triggers: Record<string, string> = { "builtin.manual": "手动", "builtin.interval": "固定间隔", "builtin.cron": "Cron", "builtin.external": "外部通知", "builtin.execution_end": "工作执行结束" };
const conditions: Record<string, string> = { "builtin.always": "无附加条件", "builtin.script": "脚本条件", "builtin.model": "模型判断", "builtin.rule": "规则判断" };

export function configurationProblem(config: PersistentTaskConfigurationDTO): string | null {
  const components = [config.trigger, config.condition, config.context, config.action];
  if (components.some((component) => !component || component.version !== "1")) return "任务包含缺失组件或不支持的组件版本，不能编辑";
  if (!config.trigger || !(config.trigger.type_id in triggers)) return `不支持的触发器：${config.trigger?.type_id}`;
  if (!config.condition || !(config.condition.type_id in conditions)) return `不支持的判断器：${config.condition?.type_id}`;
  if (config.context?.type_id !== "builtin.literal" || config.action?.type_id !== "builtin.runtime_source") return "此动作或上下文组件尚不能在当前表单编辑";
  return null;
}

function initialConfiguration(): PersistentTaskConfigurationDTO {
  return {
    name: "", trigger: { type_id: "builtin.interval", version: "1", interval_seconds: 60, timezone: "UTC" },
    condition: { type_id: "builtin.always", version: "1" }, context: { type_id: "builtin.literal", version: "1", content: "", mode: "event" },
    action: { type_id: "builtin.runtime_source", version: "1", target_uri: "", delivery_mode: "context_and_run", delivery_policy: "after_turn" },
    policies: { completion: "continuous", misfire: "skip", match_mode: "each_match" },
  };
}

export default function TaskEditor({ configuration, busy, onSave, onCancel }: {
  configuration?: PersistentTaskConfigurationDTO; busy: boolean;
  onSave: (configuration: PersistentTaskConfigurationDTO) => Promise<boolean>; onCancel: () => void;
}) {
  const [draft, setDraft] = useState(configuration ?? initialConfiguration);
  const [ruleValue, setRuleValue] = useState(JSON.stringify(configuration?.condition?.rule ? configuration.condition.rule.expected : true));
  const [error, setError] = useState<string | null>(null);
  const problem = configurationProblem(draft);
  const { trigger, condition, context, action, policies } = draft;
  if (problem || !trigger || !condition || !context || !action || !policies) return <div role="alert">{problem ?? "任务缺少策略配置"}<button onClick={onCancel}>关闭编辑</button></div>;

  const changeCondition = (typeId: string) => {
    if (typeId === "builtin.rule") setRuleValue("true");
    setDraft({ ...draft, condition: {
    type_id: typeId, version: "1",
    ...(typeId === "builtin.script" ? { script: { command: [""], timeout_seconds: 30, output_limit_bytes: 65536, memory_limit_mb: 256 } } : {}),
    ...(typeId === "builtin.model" ? { model: { thread_uri: "", instruction: "" } } : {}),
    ...(typeId === "builtin.rule" ? { rule: { pointer: "", expected: true } } : {}),
    } });
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    let next = draft;
    if (condition.rule) {
      let expected: unknown;
      try { expected = JSON.parse(ruleValue); }
      catch { setError("规则比较值必须是有效 JSON"); return; }
      next = { ...draft, condition: { ...condition, rule: { ...condition.rule, expected } } };
    }
    if (await onSave(next)) onCancel();
  };

  return <form className="persistent-task-editor" aria-label={configuration ? "编辑持久任务" : "创建持久任务"} onSubmit={(event) => void submit(event)}>
    <label>任务名称<input aria-label="任务名称" disabled={busy} required maxLength={200} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
    <fieldset disabled={busy}><legend>何时检查</legend>
      <label>触发方式<select aria-label="触发方式" value={trigger.type_id} onChange={(event) => {
        const type_id = event.target.value;
        setDraft({ ...draft, trigger: { type_id, version: "1", timezone: "UTC", ...(type_id === "builtin.interval" ? { interval_seconds: 60 } : {}), ...(type_id === "builtin.cron" ? { expression: "0 * * * *" } : {}), ...(type_id === "builtin.execution_end" ? { source_uri: "" } : {}) } });
      }}>{Object.entries(triggers).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      {trigger.type_id === "builtin.interval" ? <label>间隔（秒）<input aria-label="间隔（秒）" required type="number" min={1} value={trigger.interval_seconds} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, interval_seconds: Number(event.target.value) } })} /></label> : null}
      {trigger.type_id === "builtin.cron" ? <label>Cron 表达式<input aria-label="Cron 表达式" required value={trigger.expression} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, expression: event.target.value } })} /></label> : null}
      {["builtin.interval", "builtin.cron"].includes(trigger.type_id) ? <label>时区<input aria-label="时区" required value={trigger.timezone} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, timezone: event.target.value } })} /></label> : null}
      {trigger.type_id === "builtin.execution_end" ? <label>执行结束来源 Thread URI<input aria-label="执行结束来源 Thread URI" required value={trigger.source_uri} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, source_uri: event.target.value } })} /></label> : null}
    </fieldset>
    <fieldset disabled={busy}><legend>附加条件</legend>
      <label>判断方式<select aria-label="判断方式" value={condition.type_id} onChange={(event) => changeCondition(event.target.value)}>{Object.entries(conditions).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      {condition.script ? <>
        <p>脚本在任务所属工作区运行，命令和每个参数分别填写，不经过 shell 拼接。试运行也会实际运行脚本。</p>
        {condition.script.command.map((argument, index) => <label key={index}>{index === 0 ? "程序" : `参数 ${index}`}<input aria-label={index === 0 ? "程序" : `参数 ${index}`} required={index === 0} value={argument} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: condition.script!.command.map((value, position) => position === index ? event.target.value : value) } } })} />{index > 0 ? <button type="button" onClick={() => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: condition.script!.command.filter((_, position) => position !== index) } } })}>删除参数 {index}</button> : null}</label>)}
        <button type="button" onClick={() => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: [...condition.script!.command, ""] } } })}>添加参数</button>
        <label>执行期限（秒）<input aria-label="执行期限（秒）" type="number" required min={0.1} step="any" value={condition.script.timeout_seconds} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, timeout_seconds: Number(event.target.value) } } })} /></label>
      </> : null}
      {condition.model ? <>
        <p>检查与试运行会实际调用判断 Thread 的模型；判断 Thread 必须与工作目标及执行结束来源分离。</p>
        <label>判断 Thread URI<input aria-label="判断 Thread URI" required value={condition.model.thread_uri} onChange={(event) => setDraft({ ...draft, condition: { ...condition, model: { ...condition.model!, thread_uri: event.target.value } } })} /></label>
        <label>判断说明<textarea aria-label="判断说明" required value={condition.model.instruction} onChange={(event) => setDraft({ ...draft, condition: { ...condition, model: { ...condition.model!, instruction: event.target.value } } })} /></label>
      </> : null}
      {condition.rule ? <>
        <label>输入 JSON Pointer<input aria-label="输入 JSON Pointer" value={condition.rule.pointer} onChange={(event) => setDraft({ ...draft, condition: { ...condition, rule: { ...condition.rule!, pointer: event.target.value } } })} /></label>
        <label>比较值（JSON）<textarea aria-label="比较值（JSON）" required value={ruleValue} onChange={(event) => setRuleValue(event.target.value)} /></label>
      </> : null}
    </fieldset>
    <fieldset disabled={busy}><legend>携带内容与工作目标</legend>
      <label>提示内容<textarea aria-label="提示内容" required value={context.content} onChange={(event) => setDraft({ ...draft, context: { ...context, content: event.target.value } })} /></label>
      <label>工作 Thread URI<input aria-label="工作 Thread URI" required value={action.target_uri} onChange={(event) => setDraft({ ...draft, action: { ...action, target_uri: event.target.value } })} /></label>
      <label>动作<select aria-label="动作" value={action.delivery_mode} onChange={(event) => setDraft({ ...draft, action: { ...action, delivery_mode: event.target.value } })}><option value="context_and_run">发布内容并请求执行</option><option value="context_only">仅发布内容</option></select></label>
      <label>结束策略<select aria-label="结束策略" value={policies.completion} onChange={(event) => setDraft({ ...draft, policies: { ...policies, completion: event.target.value } })}><option value="continuous">持续等待后续触发</option><option value="after_delivery">首次投递后结束</option></select></label>
    </fieldset>
    <details><summary>高级配置</summary><fieldset disabled={busy}>
      <label>条件触发策略<select aria-label="条件触发策略" value={policies.match_mode} onChange={(event) => setDraft({ ...draft, policies: { ...policies, match_mode: event.target.value } })}><option value="each_match">每次满足</option><option value="first_match">首次满足</option><option value="rising_edge">未满足后再次满足</option></select></label>
      <label>错过计划<select aria-label="错过计划" value={policies.misfire} onChange={(event) => setDraft({ ...draft, policies: { ...policies, misfire: event.target.value } })}><option value="skip">跳过</option><option value="run_latest">只补最近一次</option><option value="catch_up">补齐计划点</option></select></label>
      <label>投递时机<select aria-label="投递时机" value={action.delivery_policy} onChange={(event) => setDraft({ ...draft, action: { ...action, delivery_policy: event.target.value } })}><option value="after_turn">当前 Turn 后</option><option value="after_tool_result">工具结果后</option><option value="after_interrupt">中断后</option></select></label>
      <label>内容模式<select aria-label="内容模式" value={context.mode ?? "event"} onChange={(event) => setDraft({ ...draft, context: { ...context, mode: event.target.value } })}><option value="event">独立事件</option><option value="state">同一来源的最新状态</option></select></label>
      {condition.script ? <>
        <label>内存上限（MiB）<input aria-label="内存上限（MiB）" required type="number" min={16} value={condition.script.memory_limit_mb} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, memory_limit_mb: Number(event.target.value) } } })} /></label>
        <label>输出上限（字节）<input aria-label="输出上限（字节）" required type="number" min={1} value={condition.script.output_limit_bytes} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, output_limit_bytes: Number(event.target.value) } } })} /></label>
      </> : null}
    </fieldset></details>
    {error ? <div role="alert">{error}</div> : null}
    <div className="persistent-task-actions"><button disabled={busy} type="submit">保存任务</button><button disabled={busy} type="button" onClick={onCancel}>取消编辑</button></div>
  </form>;
}
