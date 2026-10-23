import { useContextInspection, type InspectionOwner } from "../../hooks/contextInspection/useContextInspection";
import { objectField, textField, type ContextItem } from "../../state/contextInspection/pagination";
import "../../styles/inspection/contextInspection.css";

function ProjectionItem({ item }: { item: ContextItem }) {
  if (item.kind === "selection") {
    const data = objectField(item.data);
    const ref = objectField(data.ref);
    return <li className="context-selection-entry" data-plan-ordinal={data.plan_ordinal}
      data-ref-id={textField(ref, "ref_id")} data-included={String(data.included)}>
      <div className="context-selection-heading">
        <strong>#{String(data.plan_ordinal)} {textField(ref, "ref_id")}</strong>
        <span className={data.included ? "context-included" : "context-omitted"}>{data.included ? "已纳入" : "已省略"}</span>
      </div>
      <div>{textField(data, "selection_kind")} · {textField(ref, "ref_type")}</div>
      <div>可见性 {String(data.visibility)} · 保护 {String(data.protection)} · 可用性 {String(data.availability)}</div>
      {data.omission_reason ? <p>省略原因：{String(data.omission_reason)}</p> : null}
      <details><summary>来源与 selection manifest</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>
    </li>;
  }
  if (item.kind === "assembly") return <li className="context-assembly-header">
    <strong>冻结 assembly：{textField(item.data, "assembly_id")}</strong>
    {item.resource_refs.length ? <ul aria-label="封存资源来源">
      {item.resource_refs.map((resource) => {
        const provenance = resource.provenance_ref;
        if (!provenance) throw new Error("封存资源缺少类型化 provenance ref");
        return <li key={provenance.token} data-provenance-ref={provenance.token}>
          <div>{resource.display_uri ?? "内部语义资源"}</div>
          <div>{resource.resource_kind} · {resource.owner_scope} · {resource.facet} · {resource.availability}</div>
          <div>版本标识：{resource.revision}</div>
        </li>;
      })}
    </ul> : null}
    <details><summary>快照元数据</summary><pre>{JSON.stringify(item.data, null, 2)}</pre></details>
  </li>;
  if (item.kind === "message") return <li className="context-history-message" data-message-id={textField(item.data, "message_id")}>
    <strong>history 投影 · {item.role}</strong>
    {item.text ? <p>{item.text}</p> : null}
    {item.reasoning ? <details><summary>可见 reasoning</summary><p>{item.reasoning}</p></details> : null}
    {item.tool_summary.length ? <p>工具摘要：{item.tool_summary.join("、")}</p> : null}
  </li>;
  if (item.kind === "loss") return <li className="context-capability-loss">
    <strong>Loss · {textField(item.data, "projection")}</strong><p>{textField(item.data, "loss")}</p>
  </li>;
  throw new Error(`不支持的上下文检查记录 ${item.kind}`);
}

export default function AssemblyContextInspector(owner: InspectionOwner) {
  const state = useContextInspection(owner);
  return <section className="context-inspector" aria-label="冻结请求上下文" data-owner-session-id={owner.session?.session_id ?? ""}>
    <p>只读检查指定请求的冻结 selection 与 history 投影；不会改变当前对话历史。不加载 request-only 正文或工具定义。</p>
    <div className="context-inspector-controls">
      <label>冻结请求 <select aria-label="选择冻结 assembly" value={state.assemblyRef}
        onChange={(event) => {
          const item = state.catalog.items.find((candidate) => candidate.assembly_ref === event.target.value);
          void state.loadProjection(item ?? null, true);
        }}>
        <option value="">请选择已封存请求</option>
        {state.assemblyRef && !state.catalog.items.some((item) => item.assembly_ref === state.assemblyRef)
          ? <option value={state.assemblyRef}>当前检查：{state.assemblyId}（列表尚未加载）</option> : null}
        {state.catalog.items.map((item) => <option key={item.assembly_ref} value={item.assembly_ref ?? ""}>
          {textField(item.data, "assembly_id")} · history {String(objectField(item.data).history_view_revision)}
        </option>)}
      </select></label>
      <button type="button" disabled={state.catalogLoading} onClick={() => void state.loadCatalog(true)}>刷新请求列表</button>
      {state.catalog.hasMore ? <button type="button" disabled={state.catalogLoading} onClick={() => void state.loadCatalog()}>加载更早请求</button> : null}
    </div>
    {state.catalogLoading ? <p role="status">正在读取封存请求…</p> : null}
    {state.catalogError ? <p role="alert">请求列表加载失败：{state.catalogError}</p> : null}
    {!state.catalogLoading && !state.catalogError && !state.catalog.items.length ? <p>当前会话没有已封存请求。</p> : null}
    {state.assemblyRef ? <div className="context-projection" data-assembly-id={state.assemblyId}>
      <p>已加载 {state.projection.items.length} 项{state.projection.hasMore ? "，还有后续内容" : ""}</p>
      <ol aria-label="冻结 selection 与 history" className="context-inspection-items">
        {state.projection.items.map((item, index) => <ProjectionItem key={`${state.assemblyRef}:${index}`} item={item} />)}
      </ol>
      {state.projection.fragment ? <p>当前字段尚有分片，请继续加载以完整还原。</p> : null}
      {state.projectionLoading ? <p role="status">正在读取冻结上下文…</p> : null}
      {state.projectionError ? <div role="alert"><p>冻结上下文加载失败：{state.projectionError}</p>
        <button type="button" onClick={() => void state.loadProjection(state.selectedAssembly, true)}>重新读取冻结上下文</button></div> : null}
      {state.projection.hasMore ? <button type="button" disabled={state.projectionLoading} onClick={() => void state.loadProjection(state.selectedAssembly)}>加载下一页上下文</button> : null}
    </div> : null}
  </section>;
}
