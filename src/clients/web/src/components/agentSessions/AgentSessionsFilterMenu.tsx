import { t } from '../../../../shared/core/i18n/index.js';
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
      <div className="sessions-menu-group-title">{t('navigation.filter.sort')}</div>
      <button
        type="button"
        className={sortMode === 'created' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={sortMode === 'created'}
        onClick={() => onApplySortMode('created', t('navigation.filter.byCreated'))}
      >
        {t('navigation.filter.byCreated')}
      </button>
      <button
        type="button"
        className={sortMode === 'updated' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={sortMode === 'updated'}
        onClick={() => onApplySortMode('updated', t('navigation.filter.byUpdated'))}
      >
        {t('navigation.filter.byUpdated')}
      </button>
      <div className="sessions-menu-separator" />
      <div className="sessions-menu-group-title">{t('navigation.filter.group')}</div>
      <button
        type="button"
        className={groupingMode === 'workspace' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={groupingMode === 'workspace'}
        onClick={() => onApplyGroupingMode('workspace', t('navigation.filter.byWorkspace'))}
      >
        {t('navigation.filter.byWorkspace')}
      </button>
      <button
        type="button"
        className={groupingMode === 'time' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={groupingMode === 'time'}
        onClick={() => onApplyGroupingMode('time', t('navigation.filter.byTime'))}
      >
        {t('navigation.filter.byTime')}
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
            {t('navigation.filter.showRecent')}
          </button>
          <button
            type="button"
            className={!workspaceGroupCapped ? 'active' : ''}
            role="menuitemradio"
            aria-checked={!workspaceGroupCapped}
            onClick={() => onToggleWorkspaceGroupCapping(false)}
          >
            {t('navigation.filter.showAll')}
          </button>
        </>
      ) : null}
      <div className="sessions-menu-separator" />
      <button
        type="button"
        role="menuitem"
        onClick={onCollapseAllSessionSections}
      >
        {t('navigation.filter.collapseAll')}
      </button>
      <div className="sessions-menu-separator" />
      <div className="sessions-menu-group-title">{t('navigation.filter.title')}</div>
      <button
        type="button"
        className={filterMode === 'all' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'all'}
        onClick={() => onApplyFilterMode('all', t('navigation.filter.all'))}
      >
        {t('navigation.filter.all')}
      </button>
      <button
        type="button"
        className={filterMode === 'current' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'current'}
        onClick={() => onApplyFilterMode('current', t('navigation.filter.current'))}
      >
        {t('navigation.filter.current')}
      </button>
      <button
        type="button"
        className={filterMode === 'attachments' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'attachments'}
        onClick={() => onApplyFilterMode('attachments', t('navigation.filter.attachments'))}
      >
        {t('navigation.filter.attachments')}
      </button>
      <button
        type="button"
        className={filterMode === 'agent' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'agent'}
        onClick={() => onApplyFilterMode('agent', t('navigation.filter.agent'))}
      >
        {t('navigation.filter.agent')}
      </button>
      <button
        type="button"
        className={filterMode === 'named' ? 'active' : ''}
        role="menuitemradio"
        aria-checked={filterMode === 'named'}
        onClick={() => onApplyFilterMode('named', t('navigation.filter.named'))}
      >
        {t('navigation.filter.named')}
      </button>
    </div>
  );
}
