import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
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
      summary={t("inspection.context.preview", label)}
      renderContent={(open) => open ? <p>{value}</p> : null}
    />
  </>;
}

function ReasoningDetails({ value }: { value: string }) {
  return <ControlledDetails
    summary={t("inspection.context.reasoning")}
    renderContent={(open) => open ? <ExpandableText label={t("inspection.context.fullReasoning")} value={value} /> : null}
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
        <span className={selectionData.included ? "context-included" : "context-omitted"}>{selectionData.included ? t("inspection.context.included") : t("inspection.context.omitted")}</span>
      </div>
      <div>{textField(selectionData, "selection_kind")} · {textField(resourceRef, "ref_type")}</div>
      <div>{t("inspection.context.flags", String(selectionData.visibility), String(selectionData.protection), String(selectionData.availability))}</div>
      {selectionData.omission_reason ? <p>{t("inspection.context.omission", String(selectionData.omission_reason))}</p> : null}
      <JsonDetails summary={t("inspection.context.manifest")} value={selectionData} />
    </li>;
  }
  if (item.kind === "assembly") return <li className="context-assembly-header">
    <strong>{t("inspection.context.assembly", textField(item.data, "assembly_id"))}</strong>
    {item.resource_refs.length ? <ul aria-label={t("inspection.context.resources")}>
      {item.resource_refs.map((resource) => {
        const provenance = resource.provenance_ref;
        if (!provenance) throw new Error(t("inspection.context.missingProvenance"));
        return <li key={provenance.token} data-provenance-ref={provenance.token}>
          <div>{resource.display_uri ?? t("inspection.context.internal")}</div>
          <div>{resource.resource_kind} · {resource.owner_scope} · {resource.facet} · {resource.availability}</div>
          <div>{t("inspection.context.revision", resource.revision)}</div>
        </li>;
      })}
    </ul> : null}
    <JsonDetails summary={t("inspection.context.metadata")} value={item.data} />
  </li>;
  if (item.kind === "message") return <li className="context-history-message" data-message-id={textField(item.data, "message_id")}>
    <strong>{t("inspection.context.history", item.role)}</strong>
    {item.text ? <ExpandableText label={t("inspection.context.fullContent")} value={item.text} /> : null}
    {item.reasoning ? <ReasoningDetails value={item.reasoning} /> : null}
    {item.tool_summary.length ? <p>{t("inspection.context.tools", item.tool_summary.join(", "))}</p> : null}
  </li>;
  if (item.kind === "loss") return <li className="context-capability-loss">
    <strong>Loss · {textField(item.data, "projection")}</strong><p>{textField(item.data, "loss")}</p>
  </li>;
  throw new Error(t("inspection.context.unsupported", item.kind));
}

export default function AssemblyContextInspector(owner: InspectionOwner) {
  const inspection = useContextInspection(owner);
  return <section className="context-inspector" aria-label={t("inspection.context.aria")} data-owner-session-id={owner.session?.session_id ?? ""}>
    <p>{t("inspection.context.hint")}</p>
    <div className="context-inspector-controls">
      <label>{t("inspection.context.request")} <select aria-label={t("inspection.context.select")} value={inspection.assemblyRef}
        onChange={(event) => {
          const item = inspection.catalog.items.find((candidate) => candidate.assembly_ref === event.target.value);
          void inspection.loadProjection(item ?? null, true);
        }}>
        <option value="">{t("inspection.context.selectPrompt")}</option>
        {inspection.assemblyRef && !inspection.catalog.items.some((item) => item.assembly_ref === inspection.assemblyRef)
          ? <option value={inspection.assemblyRef}>{t("inspection.context.current", inspection.assemblyId)}</option> : null}
        {inspection.catalog.items.map((item) => <option key={item.assembly_ref} value={item.assembly_ref ?? ""}>
          {textField(item.data, "assembly_id")} · history {String(objectField(item.data).history_view_revision)}
        </option>)}
      </select></label>
      <button type="button" disabled={inspection.catalogLoading} onClick={() => void inspection.loadCatalog(true)}>{t("inspection.context.refresh")}</button>
      {inspection.catalog.hasMore ? <button type="button" disabled={inspection.catalogLoading} onClick={() => void inspection.loadCatalog()}>{t("inspection.context.older")}</button> : null}
    </div>
    {inspection.catalogLoading ? <p role="status">{t("inspection.context.loadingCatalog")}</p> : null}
    {inspection.catalogError ? <p role="alert">{t("inspection.context.catalogFailed", inspection.catalogError)}</p> : null}
    {!inspection.catalogLoading && !inspection.catalogError && !inspection.catalog.items.length ? <p>{t("inspection.context.empty")}</p> : null}
    {inspection.assemblyRef ? <div className="context-projection" data-assembly-id={inspection.assemblyId}>
      <p>{t(inspection.projection.hasMore ? "inspection.context.loadedMore" : "inspection.context.loaded", formatNumber(inspection.projection.items.length))}</p>
      <ol aria-label={t("inspection.context.projectionAria")} className="context-inspection-items">
        {inspection.projection.items.map((item, index) => <ProjectionItem key={`${inspection.assemblyRef}:${index}`} item={item} />)}
      </ol>
      {inspection.projection.fragment ? <p>{t("inspection.context.fragment")}</p> : null}
      {inspection.projectionLoading ? <p role="status">{t("inspection.context.loading")}</p> : null}
      {inspection.projectionError ? <div role="alert"><p>{t("inspection.context.failed", inspection.projectionError)}</p>
        <button type="button" onClick={() => void inspection.loadProjection(inspection.selectedAssembly, true)}>{t("inspection.context.retry")}</button></div> : null}
      {inspection.projection.hasMore ? <button type="button" disabled={inspection.projectionLoading} onClick={() => void inspection.loadProjection(inspection.selectedAssembly)}>{t("inspection.context.next")}</button> : null}
    </div> : null}
  </section>;
}
