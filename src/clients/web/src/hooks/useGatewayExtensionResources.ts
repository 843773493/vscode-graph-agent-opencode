import { useCallback, useEffect, useRef, useState } from "react";
import { listGatewayResources } from "../gatewayApi";
import type {
  GatewayResourceItem,
  GatewayResourceScopeError,
  SessionResource,
  SessionResourceAction,
} from "../types/backend";
import { controlSessionResource } from "../api";

export type GatewayExtensionRuntimeResource = Omit<SessionResource, "kind"> & {
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
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

  const control = useCallback(async (
    entry: GatewayExtensionResourceEntry,
    action: SessionResourceAction,
  ) => {
    await controlSessionResource(
      apiPort,
      entry.session_id,
      entry.resource.kind,
      entry.resource.resource_id,
      action,
      entry.workspace_id,
    );
    await refresh(true, true);
  }, [apiPort, refresh]);

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
  };
}
