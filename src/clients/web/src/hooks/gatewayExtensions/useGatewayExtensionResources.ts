import { useCallback, useEffect, useRef, useState } from "react";
import { listGatewayResources } from "../../gatewayApi";
import type {
  GatewayResourceItem,
  GatewayResourceScopeError,
  SessionResourceAction,
} from "../../types/backend";
import { controlWorkspaceResource } from "../../api/workspaceResources";
import type { ResourceAction, ResourceThreadRef, WorkspaceResourceDTO } from "../../api/workspaceResources";
import { errorMessage } from "../../utils/errorMessage";

export type GatewayExtensionRuntimeResource = Omit<WorkspaceResourceDTO, "kind"> & {
  kind: "browser" | "terminal";
};

export interface GatewayExtensionResourceEntry
  extends Omit<GatewayResourceItem, "resource"> {
  key: string;
  resource: GatewayExtensionRuntimeResource;
}

export type GatewayExtensionResourceError = GatewayResourceScopeError;

function resourceKey(item: GatewayResourceItem): string {
  return `${item.workspace_id}:${item.session_id}:${item.resource.kind}:${item.resource.resource_id}`;
}

function selectDefaultResource(
  entries: GatewayExtensionResourceEntry[],
  preferredKey: string | null,
): string | null {
  if (preferredKey && entries.some((entry) => entry.key === preferredKey)) {
    return preferredKey;
  }
  return entries.find(
    (entry) => entry.resource.kind === "browser" && entry.resource.status === "running",
  )?.key ?? entries[0]?.key ?? null;
}

export function useGatewayExtensionResources({
  apiPort,
  initialResourceKey,
  enabled,
}: {
  apiPort: number;
  initialResourceKey: string | null;
  enabled: boolean;
}) {
  const [entries, setEntries] = useState<GatewayExtensionResourceEntry[]>([]);
  const [errors, setErrors] = useState<GatewayResourceScopeError[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(initialResourceKey);
  const requestSequence = useRef(0);
  const inFlightRefreshRef = useRef<Promise<void> | null>(null);
  const initialResourceKeyRef = useRef(initialResourceKey);
  initialResourceKeyRef.current = initialResourceKey;

  const refresh = useCallback(async (silent = false, force = false) => {
    if (!enabled) {
      return;
    }
    const inFlight = inFlightRefreshRef.current;
    if (inFlight) {
      await inFlight;
      if (!force) {
        return;
      }
      // 强制刷新用于控制动作完成后重新读取；先等待旧请求结束，
      // 再检查是否已有另一个强制刷新接手，避免并发重复读取。
      if (inFlightRefreshRef.current && inFlightRefreshRef.current !== inFlight) {
        await inFlightRefreshRef.current;
        return;
      }
      if (inFlightRefreshRef.current === inFlight) {
        inFlightRefreshRef.current = null;
      }
    }
    const requestId = ++requestSequence.current;
    if (!silent) {
      setLoading(true);
    }
    const request = (async () => {
      try {
        const result = await listGatewayResources(apiPort);
        if (requestId !== requestSequence.current) {
          return;
        }
        const nextEntries: GatewayExtensionResourceEntry[] = result.items.map((item) => ({
          ...item,
          key: resourceKey(item),
          resource: item.resource as GatewayExtensionRuntimeResource,
        }));
        setEntries(nextEntries);
        setErrors(result.errors);
        setLoadedAt(new Date().toISOString());
        setSelectedKey((current) => selectDefaultResource(
          nextEntries,
          current ?? initialResourceKeyRef.current,
        ));
      } catch (error) {
        if (requestId !== requestSequence.current) {
          return;
        }
        setErrors([{
          scope_key: "gateway",
          label: "Gateway 全局资源",
          message: errorMessage(error),
        }]);
      } finally {
        if (requestId === requestSequence.current) {
          setLoading(false);
        }
      }
    })();
    inFlightRefreshRef.current = request;
    void request.then(() => {
      if (inFlightRefreshRef.current === request) {
        inFlightRefreshRef.current = null;
      }
    }, () => {
      if (inFlightRefreshRef.current === request) {
        inFlightRefreshRef.current = null;
      }
    });
    await request;
  }, [apiPort, enabled]);

  useEffect(() => {
    setSelectedKey((current) => current ?? initialResourceKey);
  }, [initialResourceKey]);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refresh(true);
      }
    }, 5000);
    return () => {
      window.clearInterval(timer);
      requestSequence.current += 1;
    };
  }, [enabled, refresh]);

  const ownerControl = useCallback(async (
    entry: GatewayExtensionResourceEntry, action: ResourceAction, thread?: ResourceThreadRef,
  ) => {
    if (action === "attach" && !thread) throw new Error("关联 Gateway 资源必须提供明确的 session_id 与 thread_id");
    // 写入先作废在途列表，完整回执不能被较早的轮询覆盖。
    ++requestSequence.current;
    try {
      const resource = await controlWorkspaceResource(apiPort, entry.workspace_id, entry.resource, action, thread);
      // 写入等待期间启动的轮询也属于旧快照；回执落地时一并作废。
      ++requestSequence.current;
      setEntries((current) => resource.status === "deleted"
        ? current.filter((item) => item.key !== entry.key)
        : current.map((item) => item.key === entry.key ? { ...item, resource: resource as GatewayExtensionRuntimeResource } : item));
      setLoading(false);
    } catch (error) {
      await refresh(true, true);
      throw error;
    }
  }, [apiPort, refresh]);

  const control = useCallback(async (entry: GatewayExtensionResourceEntry, action: SessionResourceAction) => {
    if (action !== "cancel" && action !== "delete") throw new Error("工作区资源不支持该操作");
    await ownerControl(entry, action === "cancel" ? "stop" : "delete");
  }, [ownerControl]);

  return {
    entries,
    errors,
    loading,
    loadedAt,
    selectedKey,
    selectedEntry: entries.find((entry) => entry.key === selectedKey) ?? null,
    select: setSelectedKey,
    refresh: () => refresh(false, true),
    control,
    ownerControl,
  };
}
