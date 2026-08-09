import type { WorkspaceAuxiliaryTab } from "./WorkspaceAuxiliaryPanel";

interface WorkspaceInfoSidebarProps {
  tab: Extract<WorkspaceAuxiliaryTab, "automation">;
  visible: boolean;
  flexRatio: number;
  workspaceName: string;
  workspaceRoot: string;
  sessionTitle: string;
  onToggle: () => void;
}

const INFO_COPY = {
  automation: {
    title: "自动化",
    description: "计划任务入口",
    sections: [
      ["范围", "当前会话"],
      ["状态", "待命"],
    ],
  },
} as const;

export default function WorkspaceInfoSidebar({
  tab,
  visible,
  flexRatio,
  workspaceName,
  workspaceRoot,
  sessionTitle,
  onToggle,
}: WorkspaceInfoSidebarProps) {
  const copy = INFO_COPY[tab];

  return (
    <aside
      className={`workspace-info-sidebar${visible ? "" : " collapsed"}`}
      style={{ flexBasis: visible ? 0 : "32px", flexGrow: visible ? flexRatio : 0 }}
      aria-label={`${copy.title}信息页`}
    >
      {visible ? (
        <>
          <header className="workspace-info-sidebar-header">
            <div>
              <strong>{copy.title}</strong>
              <span>{copy.description}</span>
            </div>
            <button
              type="button"
              className="workspace-info-sidebar-toggle"
              title="收起左侧信息页"
              aria-label="收起左侧信息页"
              onClick={onToggle}
            >
              <span className="codicon codicon-chevron-left" aria-hidden="true" />
            </button>
          </header>
          <div className="workspace-info-sidebar-content">
            <section className="workspace-info-context-card">
              <strong title={workspaceRoot || workspaceName}>{workspaceName}</strong>
              <span title={sessionTitle}>{sessionTitle} · 本地工作区</span>
            </section>
            <section className="workspace-info-summary" aria-label={`${copy.title}信息摘要`}>
              {copy.sections.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </section>
            <p className="workspace-info-sidebar-note">
              自动化配置作用于当前会话，不代表 Gateway 全局健康状态。
            </p>
          </div>
        </>
      ) : (
        <button
          type="button"
          className="workspace-info-sidebar-expand"
          title="展开左侧信息页"
          aria-label="展开左侧信息页"
          onClick={onToggle}
        >
          <span className="codicon codicon-chevron-right" aria-hidden="true" />
          <span>{copy.title}</span>
        </button>
      )}
    </aside>
  );
}
