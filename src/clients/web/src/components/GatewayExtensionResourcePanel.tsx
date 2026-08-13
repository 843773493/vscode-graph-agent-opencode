import { useMemo, useState } from "react";
import type { SessionResourceAction } from "../types/backend";
import type {
  GatewayExtensionResourceEntry,
  GatewayExtensionResourceError,
} from "../hooks/useGatewayExtensionResources";
import { kindLabel } from "../state/resourceDisplay";
import ResourceTreeRow from "./ResourceTreeRow";
import { useWarmConfirm } from "./WarmConfirmProvider";

interface GatewayExtensionResourcePanelProps {
  entries: GatewayExtensionResourceEntry[];
  errors: GatewayExtensionResourceError[];
  loading: boolean;
  loadedAt: string | null;
  selectedKey: string | null;
  onSelect: (key: string) => void;
  onRefresh: () => void;
  onControl: (
    entry: GatewayExtensionResourceEntry,
    action: SessionResourceAction,
  ) => Promise<void>;
  onOpen: (entry: GatewayExtensionResourceEntry) => void;
  onCreateReplacement: (entry: GatewayExtensionResourceEntry) => Promise<void>;
}

interface ResourceScopeGroup {
  key: string;
  gatewayName: string;
  workspaceName: string;
  sessionTitle: string;
  entries: GatewayExtensionResourceEntry[];
}

function groupResources(
  entries: GatewayExtensionResourceEntry[],
): ResourceScopeGroup[] {
  const groups = new Map<string, ResourceScopeGroup>();
  for (const entry of entries) {
    const key = `${entry.workspace_id}:${entry.session_id}`;
    const current = groups.get(key) ?? {
      key,
      gatewayName: entry.gateway_name,
      workspaceName: entry.workspace_name,
      sessionTitle: entry.session_title || "未命名会话",
      entries: [],
    };
    current.entries.push(entry);
    groups.set(key, current);
  }
  return [...groups.values()].sort((left, right) =>
    `${left.gatewayName}/${left.workspaceName}/${left.sessionTitle}`.localeCompare(
      `${right.gatewayName}/${right.workspaceName}/${right.sessionTitle}`,
    ),
  );
}

export default function GatewayExtensionResourcePanel({
  entries,
  errors,
  loading,
  loadedAt,
  selectedKey,
  onSelect,
  onRefresh,
  onControl,
  onOpen,
  onCreateReplacement,
}: GatewayExtensionResourcePanelProps) {
  const confirm = useWarmConfirm();
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [openScopes, setOpenScopes] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const groups = useMemo(() => groupResources(entries), [entries]);
  const groupedErrors = useMemo(() => {
    const grouped = new Map<string, { message: string; labels: string[] }>();
    for (const error of errors) {
      const current = grouped.get(error.message) ?? { message: error.message, labels: [] };
      current.labels.push(error.label);
      grouped.set(error.message, current);
    }
    return [...grouped.values()];
  }, [errors]);
  const activeCount = entries.filter((entry) => entry.resource.status === "running").length;

  const handleControl = async (
    entry: GatewayExtensionResourceEntry,
    action: SessionResourceAction,
  ) => {
    if (action === "delete") {
      const confirmed = await confirm({
        title: `删除${kindLabel(entry.resource.kind)}`,
        message: `确认删除 ${entry.resource.name}？删除后只保留历史记录。`,
        confirmText: "删除",
        danger: true,
      });
      if (!confirmed) return;
    }
    setBusyKey(entry.key);
    setNotice(null);
    try {
      await onControl(entry, action);
      setNotice(`已更新 ${entry.resource.name} 的状态`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setNotice(`操作失败：${message}`);
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <section
      className="panel-view resource-panel gateway-extension-resource-panel"
      data-extension-region="secondary"
    >
      <div className="panel-header">
        <div
          className="panel-title"
          title={loadedAt ? `最近读取 ${loadedAt}` : "Gateway 全局资源"}
        >
          Gateway 连接 <span className="resource-total-count">{entries.length}</span>
        </div>
        <div className="panel-header-meta">
          <span>{activeCount} 个运行中</span>
          <span>{groups.length} 个会话</span>
        </div>
        <button
          type="button"
          className="resource-icon-button"
          onClick={onRefresh}
          disabled={loading}
          title="刷新所有 Gateway 连接"
          aria-label="刷新所有 Gateway 连接"
        >
          <span
            className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`}
            aria-hidden="true"
          />
        </button>
      </div>
      <div className="gateway-extension-scope-hint">
        所有 Gateway、工作区和会话申请的浏览器/终端；不跟随标准窗口当前会话。
      </div>
      {entries.length > 0 ? (
        <nav className="gateway-extension-resource-tabs" aria-label="扩展窗口资源标签" role="tablist">
          {entries.map((entry) => {
            const selected = entry.key === selectedKey;
            const icon = entry.resource.kind === "browser" ? "codicon-globe" : "codicon-terminal";
            return (
              <button
                type="button"
                role="tab"
                aria-selected={selected}
                className={`gateway-extension-resource-tab${selected ? " active" : ""}`}
                key={entry.key}
                title={`${entry.resource.name} · ${entry.workspace_name} · ${entry.session_title}`}
                onClick={() => {
                  onSelect(entry.key);
                  onOpen(entry);
                }}
              >
                <span className={`codicon ${icon}`} aria-hidden="true" />
                <span>{entry.resource.name || kindLabel(entry.resource.kind)}</span>
                <span
                  className={`gateway-extension-resource-tab-state ${entry.resource.status}`}
                  aria-label={entry.resource.status === "running" ? "运行中" : entry.resource.status}
                />
              </button>
            );
          })}
        </nav>
      ) : null}
      {notice ? <div className="resource-notice" role="status">{notice}</div> : null}
      {groupedErrors.map((error) => (
        <div className="resource-notice gateway-extension-error" key={error.message}>
          <strong>{error.labels.length} 个资源范围暂不可用</strong>
          <details>
            <summary>查看范围与技术详情</summary>
            <span>{error.labels.join("、")}</span>
            <code>{error.message}</code>
          </details>
        </div>
      ))}
      {loading && entries.length === 0 ? (
        <div className="empty-state">正在读取所有 Gateway 连接...</div>
      ) : null}
      {!loading && entries.length === 0 && errors.length === 0 ? (
        <div className="empty-state">当前 Gateway 下还没有可连接的浏览器或终端。</div>
      ) : null}
      {entries.length > 0 ? (
        <div
          className="resource-tree gateway-extension-resource-tree"
          role="tree"
          aria-label="所有 Gateway 连接"
        >
          {groups.map((group) => {
            const open = openScopes[group.key] ?? true;
            return (
              <section className="gateway-extension-scope" key={group.key}>
                <button
                  type="button"
                  className="gateway-extension-scope-row"
                  aria-expanded={open}
                  onClick={() => setOpenScopes((current) => ({
                    ...current,
                    [group.key]: !open,
                  }))}
                >
                  <span
                    className={`codicon codicon-chevron-${open ? "down" : "right"}`}
                    aria-hidden="true"
                  />
                  <span className="gateway-extension-scope-copy">
                    <strong>{group.sessionTitle}</strong>
                    <small>{group.gatewayName} · {group.workspaceName}</small>
                  </span>
                  <span>{group.entries.length}</span>
                </button>
                {open ? (
                  <div className="gateway-extension-scope-children">
                    {group.entries.map((entry) => (
                      <ResourceTreeRow
                        key={entry.key}
                        resource={entry.resource}
                        selected={entry.key === selectedKey}
                        busy={busyKey === entry.key}
                        onControl={(kind, resourceId, action) => {
                          if (
                            kind !== entry.resource.kind ||
                            resourceId !== entry.resource.resource_id
                          ) {
                            throw new Error("扩展窗口资源身份不一致");
                          }
                          void handleControl(entry, action);
                        }}
                        onCopy={(resourceId) => {
                          void navigator.clipboard?.writeText(resourceId);
                        }}
                        onOpenTerminal={() => {
                          onSelect(entry.key);
                          onOpen(entry);
                        }}
                        onOpenBrowser={() => {
                          onSelect(entry.key);
                          onOpen(entry);
                        }}
                        onReplaceBrowser={() => void onCreateReplacement(entry)}
                      />
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
