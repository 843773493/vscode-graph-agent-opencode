import { useMemo, useState, type ReactNode } from "react";

import { useContextInspection, type InspectionOwner } from "../../hooks/contextInspection/useContextInspection";
import { objectField, textField, type ContextItem } from "../../state/contextInspection/pagination";
import "../../styles/inspection/contextInspection.css";
import { prettyJson } from "../../utils/jsonDisplay";

const MESSAGE_PREVIEW_CHAR_LIMIT = 2_000;

function previewText(value: string): { text: string; truncated: boolean } {
  const characters: string[] = [];
  for (const character of value) {
    if (characters.length === MESSAGE_PREVIEW_CHAR_LIMIT) {
      return { text: characters.join(""), truncated: true };
    }
    characters.push(character);
  }
  return { text: value, truncated: false };
}

function ControlledDetails({
  summary,
  renderContent,
}: {
  summary: string;
  renderContent: (open: boolean) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return <details open={open}>
    <summary onClick={(event) => {
      event.preventDefault();
      setOpen((wasOpen) => !wasOpen);
    }}>{summary}</summary>
    {renderContent(open)}
  </details>;
}

function ExpandableText({ label, value }: { label: string; value: string }) {
  const preview = useMemo(() => previewText(value), [value]);

  if (!preview.truncated) return <p>{value}</p>;
  return <>
    <p>{preview.text}…</p>
    <ControlledDetails
      summary={`${label}（正文较长，默认仅显示预览）`}
      renderContent={(open) => open ? <p>{value}</p> : null}
    />
  </>;
}

function ReasoningDetails({ value }: { value: string }) {
  return <ControlledDetails
    summary="可见 reasoning"
    renderContent={(open) => open ? <ExpandableText label="查看完整 reasoning" value={value} /> : null}
  />;
}

function JsonDetails({ summary, value }: { summary: string; value: unknown }) {
  return <ControlledDetails
    summary={summary}
    renderContent={(open) => open ? <pre>{prettyJson(value)}</pre> : null}
  />;
}

function ProjectionItem({ item }: { item: ContextItem }) {
  if (item.kind === "selection") {
    const selectionData = objectField(item.data);
    const resourceRef = objectField(selectionData.ref);
    return <li className="context-selection-entry" data-plan-ordinal={selectionData.plan_ordinal}
      data-ref-id={textField(resourceRef, "ref_id")} data-included={String(selectionData.included)}>
      <div className="context-selection-heading">
        <strong>#{String(selectionData.plan_ordinal)} {textField(resourceRef, "ref_id")}</strong>
        <span className={selectionData.included ? "context-included" : "context-omitted"}>{selectionData.included ? "已纳入" : "已省略"}</span>
      </div>
      <div>{textField(selectionData, "selection_kind")} · {textField(resourceRef, "ref_type")}</div>
      <div>可见性 {String(selectionData.visibility)} · 保护 {String(selectionData.protection)} · 可用性 {String(selectionData.availability)}</div>
      {selectionData.omission_reason ? <p>省略原因：{String(selectionData.omission_reason)}</p> : null}
      <JsonDetails summary="来源与 selection manifest" value={selectionData} />
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
    <JsonDetails summary="快照元数据" value={item.data} />
  </li>;
  if (item.kind === "message") return <li className="context-history-message" data-message-id={textField(item.data, "message_id")}>
    <strong>history 投影 · {item.role}</strong>
    {item.text ? <ExpandableText label="查看完整正文" value={item.text} /> : null}
    {item.reasoning ? <ReasoningDetails value={item.reasoning} /> : null}
    {item.tool_summary.length ? <p>工具摘要：{item.tool_summary.join("、")}</p> : null}
  </li>;
  if (item.kind === "loss") return <li className="context-capability-loss">
    <strong>Loss · {textField(item.data, "projection")}</strong><p>{textField(item.data, "loss")}</p>
  </li>;
  throw new Error(`不支持的上下文检查记录 ${item.kind}`);
}

export default function AssemblyContextInspector(owner: InspectionOwner) {
  const inspection = useContextInspection(owner);
  return <section className="context-inspector" aria-label="冻结请求上下文" data-owner-session-id={owner.session?.session_id ?? ""}>
    <p>只读检查指定请求的冻结 selection 与 history 投影；不会改变当前对话历史。不加载 request-only 正文或工具定义。</p>
    <div className="context-inspector-controls">
      <label>冻结请求 <select aria-label="选择冻结 assembly" value={inspection.assemblyRef}
        onChange={(event) => {
          const item = inspection.catalog.items.find((candidate) => candidate.assembly_ref === event.target.value);
          void inspection.loadProjection(item ?? null, true);
        }}>
        <option value="">请选择已封存请求</option>
        {inspection.assemblyRef && !inspection.catalog.items.some((item) => item.assembly_ref === inspection.assemblyRef)
          ? <option value={inspection.assemblyRef}>当前检查：{inspection.assemblyId}（列表尚未加载）</option> : null}
        {inspection.catalog.items.map((item) => <option key={item.assembly_ref} value={item.assembly_ref ?? ""}>
          {textField(item.data, "assembly_id")} · history {String(objectField(item.data).history_view_revision)}
        </option>)}
      </select></label>
      <button type="button" disabled={inspection.catalogLoading} onClick={() => void inspection.loadCatalog(true)}>刷新请求列表</button>
      {inspection.catalog.hasMore ? <button type="button" disabled={inspection.catalogLoading} onClick={() => void inspection.loadCatalog()}>加载更早请求</button> : null}
    </div>
    {inspection.catalogLoading ? <p role="status">正在读取封存请求…</p> : null}
    {inspection.catalogError ? <p role="alert">请求列表加载失败：{inspection.catalogError}</p> : null}
    {!inspection.catalogLoading && !inspection.catalogError && !inspection.catalog.items.length ? <p>当前会话没有已封存请求。</p> : null}
    {inspection.assemblyRef ? <div className="context-projection" data-assembly-id={inspection.assemblyId}>
      <p>已加载 {inspection.projection.items.length} 项{inspection.projection.hasMore ? "，还有后续内容" : ""}</p>
      <ol aria-label="冻结 selection 与 history" className="context-inspection-items">
        {inspection.projection.items.map((item, index) => <ProjectionItem key={`${inspection.assemblyRef}:${index}`} item={item} />)}
      </ol>
      {inspection.projection.fragment ? <p>当前字段尚有分片，请继续加载以完整还原。</p> : null}
      {inspection.projectionLoading ? <p role="status">正在读取冻结上下文…</p> : null}
      {inspection.projectionError ? <div role="alert"><p>冻结上下文加载失败：{inspection.projectionError}</p>
        <button type="button" onClick={() => void inspection.loadProjection(inspection.selectedAssembly, true)}>重新读取冻结上下文</button></div> : null}
      {inspection.projection.hasMore ? <button type="button" disabled={inspection.projectionLoading} onClick={() => void inspection.loadProjection(inspection.selectedAssembly)}>加载下一页上下文</button> : null}
    </div> : null}
  </section>;
}
