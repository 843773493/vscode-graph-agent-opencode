import '../../../../harness/js/workbench-locale.ts';
import { useContext, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ReactSurface } from "../../../../../src/clients/workbench/src/react/surface";
import { productStyles } from "../../../../../src/clients/workbench/src/react/productStyles";
import { OverlayRootContext } from "../../../../../src/clients/web/src/components/overlays/OverlayRootContext";
import { useAppState } from "../../../../../src/clients/web/src/hooks/app/appContext";
import WorkspaceAuxiliaryPanel, { type WorkspaceAuxiliaryTab } from "../../../../../src/clients/web/src/components/workspace/WorkspaceAuxiliaryPanel";
import { copyTextToClipboard } from "../../../../../src/clients/web/src/utils/clipboard";
import ChatTurnErrorBoundary from "../../../../../src/clients/web/src/components/chat/turn/ChatTurnErrorBoundary";

const events: string[] = [];
const diagnostics: string[] = [];
let sinkFailure = false;
const surfaces = new Map<string, ReactSurface>();
function Content({ name }: { name: string }): ReactNode {
  const [count, setCount] = useState(0);
  const [overlayVisible, setOverlayVisible] = useState(false);
  const [tab, setTab] = useState<WorkspaceAuxiliaryTab>("files");
  const [copyResult, setCopyResult] = useState("");
  const overlay = useContext(OverlayRootContext);
  if (!overlay) throw new Error("真实 ReactSurface 未提供浮层宿主");
  useEffect(() => {
    events.push(`${name}:mounted`);
    return () => { events.push(`${name}:unmounted`); };
  }, [name]);
  return <>
    <button type="button" onClick={() => setCount(value => value + 1)}>{name} 计数 {count}</button>
    <input aria-label={`${name} 输入`} />
    <button type="button" onClick={() => { void copyTextToClipboard('原草稿😀诊断').then(() => setCopyResult('copied'), () => setCopyResult('denied')); }}>{name} 复制</button>
    <output aria-label={`${name} 复制结果`}>{copyResult}</output>
    <button type="button" onClick={() => setOverlayVisible(value => !value)}>{name} 浮层</button>
    <div data-bt-surface="layout">内部布局</div>
    <div data-bt-surface="panel">内部面板</div>
    <div data-testid="expected-panel" style={{ background: "var(--bt-panel-surface)" }}>面板颜色基准</div>
    <select aria-label={`${name} 资源标签`} value={tab} onChange={event => setTab(event.target.value as WorkspaceAuxiliaryTab)}>
      {["files", "changes", "goal", "resources", "threads", "debug"].map(value => <option key={value}>{value}</option>)}
    </select>
    <WorkspaceAuxiliaryPanel visible flexRatio={1} tab={tab} apiPort={0} workspaceId={null}
      workspaceFileTreeReady={false} workspaceName="组件装具" sessionId="" sessionTitle="组件装具"
      activeFilePath={null} sessionChangesets={[]} selectedChangesetId={null} activeChangeset={null}
      sessionChangesLoading={false} sessionChangesError={null} sessionChangesLoadedAt={null}
      searchOpen={false} collapseVersion={0} expandedFileTreePaths={[]}
      onExpandedFileTreePathsChange={() => {}} resourcePanel={null} attachmentPreview={null}
      runtimePreview={null} debugPanel={null} taskPanel={null} threadPanel={null}
      onToggleSearch={() => {}} onCollapseAll={() => {}} onSelectSessionChangeset={() => {}}
      onRefreshSessionChanges={() => {}} onOpenSessionChangeFile={() => {}}
      onReviewSessionChangeFile={async () => {}} onOpenFile={() => {}} onStatusChange={() => {}} />
    {overlayVisible ? createPortal(<p role="note">{name} 的浮层</p>, overlay) : null}
  </>;
}

function ProviderLessConsumer(): ReactNode {
  return <span>{useAppState().state.status}</span>;
}

function FailingProvider(): ReactNode {
  useState(() => { throw new Error(`fixture-provider-initialization-failed：${"完整原因保留在诊断中，窄窗口只显示短摘要。".repeat(20)}`); });
  return null;
}

function mount(name: string, children: ReactNode): void {
  surfaces.get(name)?.dispose();
  const container = document.getElementById(name);
  if (!container) throw new Error(`缺少测试区域 ${name}`);
  const surface = new ReactSurface(container, productStyles, message => {
    if (sinkFailure) throw new Error('fixture-diagnostic-sink-unavailable');
    diagnostics.push(message);
  });
  surfaces.set(name, surface);
  surface.render(children);
}

window.reactSurfaceHarness = {
  events,
  diagnostics,
  failSink: () => { sinkFailure = true; },
  mount: () => { for (const name of ["first", "second"]) mount(name, <Content name={name} />); },
  failContext: () => { surfaces.get("first")!.render(<ProviderLessConsumer />); },
  failProvider: () => { surfaces.get("first")!.render(<FailingProvider />); },
  failTurn: () => { surfaces.get("first")!.render(<ChatTurnErrorBoundary conversationId="conversation-original"><FailingProvider /></ChatTurnErrorBoundary>); },
  recover: () => { mount("first", <Content name="first" />); },
  focus: () => { surfaces.get("first")!.focus("input"); },
  dispose: () => { surfaces.get("first")!.dispose(); surfaces.delete("first"); },
};

declare global {
  interface Window {
    reactSurfaceHarness: {
      events: string[];
      diagnostics: string[];
      failSink: () => void;
      mount: () => void;
      failContext: () => void;
      failProvider: () => void;
      failTurn: () => void;
      recover: () => void;
      focus: () => void;
      dispose: () => void;
    };
  }
}
