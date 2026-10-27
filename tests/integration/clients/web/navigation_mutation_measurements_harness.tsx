import React, { useCallback, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import WorkspaceFileTree from "../../../../src/clients/web/src/components/workspace/fileTree/WorkspaceFileTree";
import { useSessionResourceExplorer } from "../../../../src/clients/web/src/hooks/session/useSessionResourceExplorer";
import type { WorkspaceFileOperationReceipt } from "../../../../src/clients/web/src/types/backend";
import "../../../../src/clients/web/src/index.css";
import "../../../../src/clients/web/src/styles/foundation/theme.css";
import "../../../../src/clients/web/src/styles/foundation/panelShared.css";
import "../../../../src/clients/web/src/styles/session/agentSessionsPanel.css";
import "../../../../src/clients/web/src/styles/foundation/workbenchLayout.css";

interface MeasurementConfig {
  gatewayPort: number;
  workspaceId: string;
  backendWorkspaceId: string;
  workspaceRoot: string;
  workspaceUri: string;
  sessionId: string;
  workspaceNodeId: string;
  gatewayFolderIds: string[];
  expandedCount: number;
}

interface ConsumedFileTerminal {
  operation_id: string;
  client_sequence: number;
  queue_seq: number;
  receipt_revision: number;
  state: string;
  effect: string;
  response_url: string;
  target_path_title: string | null;
  ordinal: number;
  at_ms: number;
}

interface FileStatusEvent {
  message: string;
  at_ms: number;
  completed_count: number;
  ordinal: number;
  terminal: ConsumedFileTerminal | null;
  attribution_error: string | null;
}

interface FileUiSnapshot {
  observation_source: "mounted_mutation_observer";
  snapshot_index: number;
  event_index: number;
  operation_id: string;
  client_sequence: number;
  queue_seq: number;
  receipt_revision: number;
  message: string;
  completed_count: number;
  browser_time_origin_ms: number;
  browser_now_ms: number;
  browser_epoch_ms: number;
  target_path_title: string;
  target_row_visible: true;
  event: FileStatusEvent;
}

interface FileUiDiagnosticObservation {
  stage: "inspect_rejected" | "double_raf_rejected";
  browser_time_origin_ms: number;
  browser_epoch_ms: number;
  operation_id: string;
  client_sequence: number;
  event_index: number;
  event_ordinal: number;
  status_message: string | null;
  completed_count: number;
  rejection_conditions: string[];
  target_path_title: string | null;
  target_row_visible: boolean | null;
  target_row_rejection_conditions: string[];
}

const fileStatusEvents: FileStatusEvent[] = [];
const pendingTerminals: ConsumedFileTerminal[] = [];
const fileUiSnapshots: Record<string, FileUiSnapshot> = Object.create(null);
const fileUiSnapshotOrder: FileUiSnapshot[] = [];
const fileUiDiagnosticObservations: FileUiDiagnosticObservation[] = [];
const observedCompletionEvents = new Map<number, FileStatusEvent>();
const pendingUiPaints = new Set<number>();
let observationOrdinal = 0;
let fileStatusObserverReady = false;
// 仅组合真实 Response 的正文消费方法；请求、响应值和业务处理均保持原链路。
window.fetch = new Proxy(window.fetch, { async apply(target, _this, args) {
  const response: Response = await Reflect.apply(target, window, args);
  const url = new URL(response.url);
  if (!response.ok || !/^\/api\/v1\/workspace\/files\/(operations|entries|paste|copy|upload)$/.test(url.pathname)) {
    return response;
  }
  const recordConsumed = (body: unknown) => {
    const data = (body as { data?: WorkspaceFileOperationReceipt | { operations: WorkspaceFileOperationReceipt[] } }).data;
    if (!data) throw new Error("文件响应消费观察缺少真实 data");
    const receipts = "operations" in data ? data.operations : [data];
    const requested = url.searchParams.getAll("operation_id");
    for (const receipt of receipts) {
      if (receipt.state !== "committed" || receipt.effect !== "complete") continue;
      if (url.pathname.endsWith("/operations")
        && (requested.length !== 1 || requested[0] !== receipt.operation_id)) {
        throw new Error("文件终态消费与原查询 operation_id 不一致");
      }
      const publishedItems = receipt.items.filter((item) => item.state === "published");
      let targetPathTitle: string | null = null;
      for (const snapshot of receipt.snapshots) {
        const item = snapshot.items.find((entry) => publishedItems.some((published) =>
          entry.kind === published.kind && entry.path === published.path));
        if (item) {
          targetPathTitle = item.path;
          break;
        }
      }
      pendingTerminals.push({ operation_id: receipt.operation_id,
        client_sequence: receipt.client_sequence, queue_seq: receipt.queue_seq,
        receipt_revision: receipt.receipt_revision, state: receipt.state, effect: receipt.effect,
        response_url: response.url, target_path_title: targetPathTitle,
        ordinal: ++observationOrdinal, at_ms: performance.now() });
    }
  };
  return new Proxy(response, {
    get(target, key) {
      if (key === "text") return async () => {
        const text = await target.text();
        recordConsumed(JSON.parse(text));
        return text;
      };
      if (key === "json") return async () => {
        const body: unknown = await target.json();
        recordConsumed(body);
        return body;
      };
      const value: unknown = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
} });

declare global {
  interface Window {
    measurementConfig: MeasurementConfig;
    navigationHarness: { ready: boolean; fileStatusEvents: ReadonlyArray<FileStatusEvent>;
    observerReady: boolean; pendingTerminals: ReadonlyArray<ConsumedFileTerminal>;
      fileUiSnapshots: Readonly<Record<string, FileUiSnapshot>>;
      fileUiSnapshotOrder: ReadonlyArray<FileUiSnapshot>;
      fileUiDiagnosticObservations: ReadonlyArray<FileUiDiagnosticObservation>;
    };
    gatewayMutation: {
      createFolder: (name: string) => Promise<void>;
      moveWorkspace: (parent: string | null) => Promise<void>;
      nodes: () => ReadonlyArray<{ node_id: string; name: string; parent_node_id?: string | null }>;
    };
  }
}

const config = window.measurementConfig;
const workspaces = [{ workspace_id: config.workspaceId,
  backend_workspace_id: config.backendWorkspaceId, connection_kind: "local" as const }];
const catalogSyncKeys = new Map<string, string>();
const catalogRefreshVersions = new Map<string, number>();

/** 局部真实客户端 Integration：完整文件树与 Gateway 导航 hook 访问真实服务。 */
function MeasurementHarness() {
  const [fileStatus, setFileStatus] = useState("");
  const [fileCompletionCount, setFileCompletionCount] = useState(0);
  const fileCompletionCountRef = useRef(0);
  // 与主窗口的稳定状态 setter 一致，状态更新不能触发文件树资源重载。
  const handleFileStatusChange = useCallback((message: string) => {
    setFileStatus(message);
    let terminal: ConsumedFileTerminal | null = null;
    let attributionError: string | null = null;
    if (message === "文件操作已完成") {
      setFileCompletionCount(++fileCompletionCountRef.current);
      // 真实 hook 每轮串行消费一个 receipt，再完成其目录同步/remove/status。
      // 只有唯一未配对的实际终态消费可归属本回调；歧义必须显式拒绝，不能猜最近时间。
      if (pendingTerminals.length === 1) terminal = pendingTerminals[0];
      else attributionError = `完成回调有 ${pendingTerminals.length} 个未配对终态消费`;
      pendingTerminals.length = 0;
    }
    fileStatusEvents.push({ message, at_ms: performance.now(), completed_count: fileCompletionCountRef.current,
      ordinal: ++observationOrdinal, terminal, attribution_error: attributionError });
  }, []);
  useLayoutEffect(() => {
    const status = document.getElementById("file-status");
    if (!status) throw new Error("文件状态DOM尚未挂载，不能安装量化观察器");
    let active = true;
    const targetRowState = (title: string) => {
      const tree = document.querySelector<HTMLElement>('[role="tree"]');
      const row = tree && [...tree.querySelectorAll<HTMLElement>("[title]")]
        .find((element) => element.getAttribute("title") === title);
      if (!tree || !row?.isConnected) return { visible: false,
        rejection_conditions: [!tree ? "tree_not_mounted" : "target_row_not_mounted"] };
      const scroller = row.closest<HTMLElement>("[data-virtuoso-scroller]") ?? tree;
      const bounds = row.getBoundingClientRect();
      const viewport = scroller.getBoundingClientRect();
      const style = getComputedStyle(row);
      const rejectionConditions = [];
      if (style.display === "none") rejectionConditions.push("target_row_display_none");
      if (style.visibility === "hidden") rejectionConditions.push("target_row_visibility_hidden");
      if (bounds.width <= 0) rejectionConditions.push("target_row_width_zero");
      if (bounds.height <= 0) rejectionConditions.push("target_row_height_zero");
      if (bounds.top < viewport.top) rejectionConditions.push("target_row_above_viewport");
      if (bounds.bottom > viewport.bottom) rejectionConditions.push("target_row_below_viewport");
      return { visible: rejectionConditions.length === 0, rejection_conditions: rejectionConditions };
    };
    const recordRejection = (stage: FileUiDiagnosticObservation["stage"], event: FileStatusEvent,
      eventIndex: number, statusMessage: string | null, completedCount: number,
      rejectionConditions: string[], rowState: ReturnType<typeof targetRowState>) => {
      const terminal = event.terminal;
      if (!terminal?.operation_id || !Number.isFinite(terminal.client_sequence) || !rejectionConditions.length) return;
      if (fileUiDiagnosticObservations.some((entry) => entry.stage === stage
        && entry.event_ordinal === event.ordinal)) return;
      const now = performance.now();
      const observation: FileUiDiagnosticObservation = { stage,
        browser_time_origin_ms: performance.timeOrigin, browser_epoch_ms: performance.timeOrigin + now,
        operation_id: terminal.operation_id, client_sequence: terminal.client_sequence,
        event_index: eventIndex, event_ordinal: event.ordinal,
        status_message: statusMessage, completed_count: Number.isFinite(completedCount) ? completedCount : 0,
        rejection_conditions: [...rejectionConditions], target_path_title: terminal.target_path_title,
        target_row_visible: rowState?.visible ?? null,
        target_row_rejection_conditions: rowState ? [...rowState.rejection_conditions] : [] };
      if (fileUiDiagnosticObservations.length === 12) fileUiDiagnosticObservations.shift();
      fileUiDiagnosticObservations.push(observation);
    };
    const startPaint = (event: FileStatusEvent, eventIndex: number, targetTitle: string) => {
      if (pendingUiPaints.has(event.ordinal) || fileUiSnapshots[event.terminal!.operation_id]) return;
      pendingUiPaints.add(event.ordinal);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        pendingUiPaints.delete(event.ordinal);
        if (!active) return;
        const current = document.getElementById("file-status");
        const completedCount = Number(current?.dataset.completedCount);
        const now = performance.now();
        const currentEvent = fileStatusEvents[eventIndex];
        const rowState = targetRowState(targetTitle);
        const rejectionConditions = [];
        if (current?.textContent !== "文件操作已完成") rejectionConditions.push("status_message_changed");
        if (completedCount !== event.completed_count) rejectionConditions.push("completed_count_changed");
        if (currentEvent?.ordinal !== event.ordinal) rejectionConditions.push("status_event_replaced");
        if (currentEvent?.terminal?.operation_id !== event.terminal?.operation_id) {
          rejectionConditions.push("status_event_operation_changed");
        }
        if (event.terminal?.target_path_title !== targetTitle) rejectionConditions.push("target_path_title_mismatch");
        rejectionConditions.push(...rowState.rejection_conditions);
        if (rejectionConditions.length) {
          recordRejection("double_raf_rejected", event, eventIndex, current?.textContent ?? null,
            completedCount, rejectionConditions, rowState);
          return;
        }
        const operationId = event.terminal!.operation_id;
        if (fileUiSnapshots[operationId]) throw new Error(`同一 operation 出现重复DOM确认快照: ${operationId}`);
        const frozenEvent = Object.freeze({ ...event,
          terminal: event.terminal ? Object.freeze({ ...event.terminal }) : null });
        const snapshot: FileUiSnapshot = Object.freeze({
          observation_source: "mounted_mutation_observer",
          snapshot_index: fileUiSnapshotOrder.length,
          event_index: eventIndex,
          operation_id: event.terminal!.operation_id,
          client_sequence: event.terminal!.client_sequence,
          queue_seq: event.terminal!.queue_seq,
          receipt_revision: event.terminal!.receipt_revision,
          message: "文件操作已完成",
          completed_count: completedCount,
          browser_time_origin_ms: performance.timeOrigin,
          browser_now_ms: now,
          browser_epoch_ms: performance.timeOrigin + now,
          target_path_title: targetTitle,
          target_row_visible: true,
          event: frozenEvent,
        });
        Object.defineProperty(fileUiSnapshots, operationId, { value: snapshot, enumerable: true,
          writable: false, configurable: false });
        fileUiSnapshotOrder.push(snapshot);
      }));
    };
    const inspect = () => {
      const current = document.getElementById("file-status");
      if (current?.textContent !== "文件操作已完成") return;
      const completedCount = Number(current.dataset.completedCount);
      for (const [eventIndex, event] of observedCompletionEvents) {
        const terminal = event.terminal;
        const confirmedTerminal = event.message === "文件操作已完成" && event.completed_count === completedCount
          && !event.attribution_error && terminal?.state === "committed" && terminal.effect === "complete";
        const targetTitle = terminal?.target_path_title;
        const rowState = confirmedTerminal && targetTitle ? targetRowState(targetTitle) : null;
        if (rowState && !rowState.visible) {
          recordRejection("inspect_rejected", event, eventIndex, current.textContent, completedCount,
            rowState.rejection_conditions, rowState);
        }
        if (confirmedTerminal && targetTitle && rowState?.visible) startPaint(event, eventIndex, targetTitle);
      }
    };
    const observer = new MutationObserver(() => {
      const current = document.getElementById("file-status");
      const count = Number(current?.dataset.completedCount);
      if (current?.textContent === "文件操作已完成") {
        fileStatusEvents.forEach((event, index) => {
          if (event.message === "文件操作已完成" && event.completed_count === count
            && event.terminal && !event.attribution_error) observedCompletionEvents.set(index, event);
        });
      }
      inspect();
    });
    observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["data-completed-count", "title"] });
    document.addEventListener("scroll", inspect, true);
    fileStatusObserverReady = true;
    if (window.navigationHarness) {
      window.navigationHarness.observerReady = true;
      window.navigationHarness.ready = Boolean(window.gatewayMutation?.nodes().length);
    }
    return () => {
      active = false;
      observer.disconnect();
      document.removeEventListener("scroll", inspect, true);
      fileStatusObserverReady = false;
    };
  }, []);
  const [gatewayStatus, setGatewayStatus] = useState("");
  const [name, setName] = useState("");
  const [folder, setFolder] = useState(config.gatewayFolderIds[0]);
  const [expanded, setExpanded] = useState([
    config.workspaceUri,
    ...Array.from({ length: config.expandedCount }, (_, index) => (
      `${config.workspaceUri}/d${String(index).padStart(3, "0")}`
    )),
  ]);
  const explorer = useSessionResourceExplorer({
    apiPort: config.gatewayPort, workspaces, activeWorkspaceId: config.workspaceId,
    searchOpen: false, searchQuery: "", currentSessionId: "",
    workspaceNavigationSyncKey: "measurement", catalogSyncKeys,
    catalogRefreshVersions,
  });
  window.navigationHarness = { ready: Boolean(explorer.navigation?.nodes.length && fileStatusObserverReady),
    observerReady: fileStatusObserverReady, fileStatusEvents, pendingTerminals, fileUiSnapshots,
    fileUiSnapshotOrder, fileUiDiagnosticObservations };
  window.gatewayMutation = {
    createFolder: async (value) => {
      await explorer.createWorkspaceFolder(value, null);
      setGatewayStatus(`已创建:${value}`);
    },
    moveWorkspace: async (parent) => {
      await explorer.placeWorkspaceNode(config.workspaceNodeId, parent, "last");
      setGatewayStatus(`已移动:${parent ?? "root"}`);
    },
    nodes: () => explorer.navigation?.nodes ?? [],
  };
  return <main>
    <section style={{ display: "flex", flexDirection: "column", height: 8000, width: 900 }}>
      <WorkspaceFileTree active apiPort={config.gatewayPort} workspaceId={config.workspaceId}
        workspaceName="量化工作区" workspaceRoot={config.workspaceRoot} sessionId={config.sessionId}
        activeFilePath={null} searchOpen={false} collapseVersion={0} expandedPaths={expanded}
        onExpandedPathsChange={setExpanded} onCloseSearch={() => undefined}
        onOpenFile={() => undefined} onStatusChange={handleFileStatusChange} />
    </section>
    <output id="file-status" data-completed-count={fileCompletionCount}>{fileStatus}</output>
    <section id="gateway-controls" style={{ position: "fixed", right: 0, top: 0, background: "white" }}>
      <input id="gateway-name" value={name} onChange={(event) => setName(event.target.value)} />
      <button id="gateway-create" onClick={() => void window.gatewayMutation.createFolder(name).catch((error: Error) => setGatewayStatus(error.message))}>新建工作区文件夹</button>
      <select id="gateway-parent" value={folder} onChange={(event) => setFolder(event.target.value)}>
        {config.gatewayFolderIds.map((id) => <option key={id} value={id}>{id}</option>)}
      </select>
      <button id="gateway-move" onClick={() => void window.gatewayMutation.moveWorkspace(folder).catch((error: Error) => setGatewayStatus(error.message))}>移动工作区</button>
      <output id="gateway-status">{gatewayStatus}</output>
    </section>
    <ul id="gateway-navigation" data-revision={explorer.navigation?.revision}>
      {explorer.navigation?.nodes.map((node) => <li key={node.node_id} data-node-id={node.node_id} data-kind={node.kind}
        data-workspace-id={node.workspace_id ?? ""} data-parent={node.parent_node_id ?? "root"}>{node.name}</li>)}
    </ul>
  </main>;
}

createRoot(document.getElementById("root")!).render(<MeasurementHarness />);
