import { useCallback, useEffect, useRef, useState } from "react";
import { readSessionContext, sessionContextResource } from "../../api/session/sessionContext";
import { appendInspectionPage, emptyInspectionPage } from "../../state/contextInspection/pagination";
import { errorMessage } from "../../utils/errorMessage";

export interface InspectionOwner {
  port: number;
  workspaceId: string;
  sessionId: string;
  active: boolean;
}

export function useContextInspection({ port, workspaceId, sessionId, active }: InspectionOwner) {
  const [catalog, setCatalog] = useState(emptyInspectionPage);
  const [projection, setProjection] = useState(emptyInspectionPage);
  const [assemblyId, setAssemblyId] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [projectionLoading, setProjectionLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [projectionError, setProjectionError] = useState<string | null>(null);
  const catalogRef = useRef(catalog);
  const projectionRef = useRef(projection);
  const catalogRequest = useRef<AbortController | null>(null);
  const projectionRequest = useRef<AbortController | null>(null);
  const selectedRef = useRef("");

  const loadCatalog = useCallback(async (reset = false) => {
    catalogRequest.current?.abort();
    const controller = new AbortController();
    catalogRequest.current = controller;
    const previous = reset ? emptyInspectionPage() : catalogRef.current;
    if (reset) { catalogRef.current = previous; setCatalog(previous); }
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const resource = sessionContextResource(sessionId);
      const page = await readSessionContext(port, workspaceId, resource, "assemblies", {
        signal: controller.signal, cursor: previous.nextCursor, revision: previous.revision,
      });
      if (controller.signal.aborted) return;
      const next = appendInspectionPage(previous, page, resource);
      catalogRef.current = next;
      setCatalog(next);
    } catch (error) {
      if (!controller.signal.aborted) setCatalogError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setCatalogLoading(false);
    }
  }, [port, workspaceId, sessionId]);

  const loadProjection = useCallback(async (id: string, reset = false) => {
    projectionRequest.current?.abort();
    const controller = new AbortController();
    projectionRequest.current = controller;
    const previous = reset ? emptyInspectionPage() : projectionRef.current;
    setAssemblyId(id);
    selectedRef.current = id;
    if (reset) { projectionRef.current = previous; setProjection(previous); }
    setProjectionError(null);
    if (!id) { setProjectionLoading(false); return; }
    setProjectionLoading(true);
    try {
      const resource = sessionContextResource(sessionId, id);
      const page = await readSessionContext(port, workspaceId, resource, "assembly", {
        signal: controller.signal, cursor: previous.nextCursor, revision: previous.revision,
      });
      if (controller.signal.aborted) return;
      const next = appendInspectionPage(previous, page, resource);
      projectionRef.current = next;
      setProjection(next);
    } catch (error) {
      if (!controller.signal.aborted) setProjectionError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setProjectionLoading(false);
    }
  }, [port, workspaceId, sessionId]);

  useEffect(() => {
    if (active) {
      void loadCatalog(true);
      if (selectedRef.current) void loadProjection(selectedRef.current, true);
    } else {
      setCatalogLoading(false);
      setProjectionLoading(false);
    }
    return () => { catalogRequest.current?.abort(); projectionRequest.current?.abort(); };
  }, [active, loadCatalog, loadProjection]);

  return { catalog, projection, assemblyId, catalogLoading, projectionLoading, catalogError, projectionError, loadCatalog, loadProjection };
}
