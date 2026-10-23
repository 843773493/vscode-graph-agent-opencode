import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  readSessionContext,
  type SessionContextSession,
} from "../../api/session/sessionContext";
import {
  appendInspectionPage,
  emptyInspectionPage,
  textField,
  type ContextItem,
} from "../../state/contextInspection/pagination";
import { errorMessage } from "../../utils/errorMessage";

export interface InspectionOwner {
  port: number;
  gatewayWorkspaceId: string;
  session: SessionContextSession | null;
  active: boolean;
}

export function useContextInspection({ port, gatewayWorkspaceId, session: currentSession, active }: InspectionOwner) {
  // Session 状态镜像刷新不改变资源身份，不能因此取消分页或重置用户的选择。
  const session = useMemo(() => currentSession, [
    currentSession?.session_id, currentSession?.resource_identity, currentSession?.uri, currentSession?.scope,
  ]);
  const [catalog, setCatalog] = useState(emptyInspectionPage);
  const [projection, setProjection] = useState(emptyInspectionPage);
  const [selectedAssembly, setSelectedAssembly] = useState<ContextItem | null>(null);
  const [assemblyId, setAssemblyId] = useState("");
  const [assemblyRef, setAssemblyRef] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [projectionLoading, setProjectionLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [projectionError, setProjectionError] = useState<string | null>(null);
  const catalogRef = useRef(catalog);
  const projectionRef = useRef(projection);
  const catalogRequest = useRef<AbortController | null>(null);
  const projectionRequest = useRef<AbortController | null>(null);
  const selectedRef = useRef<ContextItem | null>(null);

  const loadCatalog = useCallback(async (reset = false) => {
    catalogRequest.current?.abort();
    const controller = new AbortController();
    catalogRequest.current = controller;
    const previousPage = reset ? emptyInspectionPage() : catalogRef.current;
    if (reset) { catalogRef.current = previousPage; setCatalog(previousPage); }
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      if (!session) throw new Error("上下文检查缺少当前 Session API 响应");
      const page = await readSessionContext(port, gatewayWorkspaceId, session, "assemblies", {
        signal: controller.signal, cursor: previousPage.nextCursor, revision: previousPage.revision,
      });
      if (controller.signal.aborted) return;
      if (page.items.some((item) => item.kind !== "assembly" || !item.assembly_ref)) {
        throw new Error("封存 assembly 列表项缺少 assembly_ref");
      }
      const next = appendInspectionPage(previousPage, page, session);
      catalogRef.current = next;
      setCatalog(next);
    } catch (error) {
      if (!controller.signal.aborted) setCatalogError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setCatalogLoading(false);
    }
  }, [port, gatewayWorkspaceId, session]);

  const loadProjection = useCallback(async (item: ContextItem | null, reset = false) => {
    projectionRequest.current?.abort();
    const controller = new AbortController();
    projectionRequest.current = controller;
    const previousPage = reset ? emptyInspectionPage() : projectionRef.current;
    const selectedAssemblyId = item ? textField(item.data, "assembly_id") : "";
    const selectedAssemblyRef = item?.assembly_ref ?? "";
    setSelectedAssembly(item);
    setAssemblyId(selectedAssemblyId);
    setAssemblyRef(selectedAssemblyRef);
    selectedRef.current = item;
    if (reset) { projectionRef.current = previousPage; setProjection(previousPage); }
    setProjectionError(null);
    if (!item) { setProjectionLoading(false); return; }
    if (item.kind !== "assembly" || !selectedAssemblyId || !selectedAssemblyRef) {
      setProjectionError("封存 assembly 响应缺少 assembly_id 或 assembly_ref");
      setProjectionLoading(false);
      return;
    }
    setProjectionLoading(true);
    try {
      if (!session) throw new Error("上下文检查缺少当前 Session API 响应");
      if (controller.signal.aborted) return;
      const page = await readSessionContext(port, gatewayWorkspaceId, session, "assembly", {
        signal: controller.signal, cursor: previousPage.nextCursor, revision: previousPage.revision,
        assemblyRef: selectedAssemblyRef,
      });
      if (controller.signal.aborted) return;
      const next = appendInspectionPage(previousPage, page, session);
      projectionRef.current = next;
      setProjection(next);
    } catch (error) {
      if (!controller.signal.aborted) setProjectionError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted) setProjectionLoading(false);
    }
  }, [port, gatewayWorkspaceId, session]);

  useEffect(() => {
    if (active) {
      void loadCatalog(true);
      if (selectedRef.current) {
        void loadProjection(selectedRef.current, true);
      }
    } else {
      setCatalogLoading(false);
      setProjectionLoading(false);
    }
    return () => { catalogRequest.current?.abort(); projectionRequest.current?.abort(); };
  }, [active, loadCatalog, loadProjection]);

  return { catalog, projection, selectedAssembly, assemblyId, assemblyRef, catalogLoading, projectionLoading, catalogError, projectionError, loadCatalog, loadProjection };
}
