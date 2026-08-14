import type {
  SessionFilterMode,
  SessionGroupingMode,
  SessionSortMode,
} from './agentSessionsUtils';

interface AgentSessionsFilterMenuProps {
  filterMode: SessionFilterMode;
  sortMode: SessionSortMode;
  groupingMode: SessionGroupingMode;
  workspaceGroupCapped: boolean;
  onApplyFilterMode: (mode: SessionFilterMode, label: string) => void;
  onApplySortMode: (mode: SessionSortMode, label: string) => void;
  onApplyGroupingMode: (mode: SessionGroupingMode, label: string) => void;
  onToggleWorkspaceGroupCapping: (capped: boolean) => void;
  onCollapseAllSessionSections: () => void;
}

export default function AgentSessionsFilterMenu({
  filterMode,
  sortMode,
  groupingMode,
  workspaceGroupCapped,
  onApplyFilterMode,
  onApplySortMode,
  onApplyGroupingMode,
  onToggleWorkspaceGroupCapping,
  onCollapseAllSessionSections,
}: AgentSessionsFilterMenuProps) {
  return (
    <div className="sessions-filter-menu" role="menu">
      <div className="sessions-menu-group-title">排序</div>
      <button
        type="button"
        className={sortMode === 'created' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={sortMode === 'created'}
        onClick={() => onApplySortMode('created', '按创建时间')}
      >
        按创建时间
      </button>
      <button
        type="button"
        className={sortMode === 'updated' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={sortMode === 'updated'}
        onClick={() => onApplySortMode('updated', '按更新时间')}
      >
        按更新时间
      </button>
      <div className="sessions-menu-separator" />
      <div className="sessions-menu-group-title">分组</div>
      <button
        type="button"
        className={groupingMode === 'workspace' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={groupingMode === 'workspace'}
        onClick={() => onApplyGroupingMode('workspace', '按工作区')}
      >
        按工作区
      </button>
      <button
        type="button"
        className={groupingMode === 'time' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={groupingMode === 'time'}
        onClick={() => onApplyGroupingMode('time', '按时间')}
      >
        按时间
      </button>
      {groupingMode === 'workspace' ? (
        <>
          <div className="sessions-menu-separator" />
          <button
            type="button"
            className={workspaceGroupCapped ? 'active' : ''}
            role="menuitemradio"
            aria-checked={workspaceGroupCapped}
            onClick={() => onToggleWorkspaceGroupCapping(true)}
          >
            显示最近会话
          </button>
          <button
            type="button"
            className={!workspaceGroupCapped ? 'active' : ''}
            role="menuitemradio"
            aria-checked={!workspaceGroupCapped}
            onClick={() => onToggleWorkspaceGroupCapping(false)}
          >
            显示全部会话
          </button>
        </>
      ) : null}
      <div className="sessions-menu-separator" />
      <button
        type="button"
        role="menuitem"
        onClick={onCollapseAllSessionSections}
      >
        全部折叠
      </button>
      <div className="sessions-menu-separator" />
      <div className="sessions-menu-group-title">筛选</div>
      <button
        type="button"
        className={filterMode === 'all' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'all'}
        onClick={() => onApplyFilterMode('all', '全部会话')}
      >
        全部会话
      </button>
      <button
        type="button"
        className={filterMode === 'current' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'current'}
        onClick={() => onApplyFilterMode('current', '当前会话')}
      >
        当前会话
      </button>
      <button
        type="button"
        className={filterMode === 'attachments' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'attachments'}
        onClick={() => onApplyFilterMode('attachments', '包含附件')}
      >
        包含附件
      </button>
      <button
        type="button"
        className={filterMode === 'agent' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'agent'}
        onClick={() => onApplyFilterMode('agent', '当前 Agent')}
      >
        当前 Agent
      </button>
      <button
        type="button"
        className={filterMode === 'named' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'named'}
        onClick={() => onApplyFilterMode('named', '已命名会话')}
      >
        已命名会话
      </button>
    </div>
  );
}
