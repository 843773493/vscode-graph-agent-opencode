import { useEffect, useMemo, useState } from 'react';
import type { MouseEvent } from 'react';
import type { Session } from '../types';
import { formatTime } from '../utils/format';

interface SessionTreeProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  onSetSessionParent: (sessionId: string, parentSessionId: string | null) => Promise<void>;
}

interface SessionTreeNode {
  session: Session;
  children: SessionTreeNode[];
}

interface SessionMenuState {
  sessionId: string;
  parentSessionId: string | null;
  x: number;
  y: number;
}

const DEFAULT_VISIBLE_ROOT_COUNT = 5;

function countTreeSessions(node: SessionTreeNode): number {
  return 1 + node.children.reduce((total, child) => total + countTreeSessions(child), 0);
}

function buildSessionTree(sessions: Session[]): SessionTreeNode[] {
  const nodes = new Map<string, SessionTreeNode>();
  for (const session of sessions) {
    nodes.set(session.session_id, { session, children: [] });
  }

  for (const session of sessions) {
    const ancestors = new Set<string>();
    let cursor: Session | undefined = session;
    while (cursor) {
      if (ancestors.has(cursor.session_id)) {
        throw new Error(`会话树存在循环绑定：${cursor.session_id}`);
      }
      ancestors.add(cursor.session_id);
      const parentId: string | null = cursor.parent_session_id;
      cursor = parentId ? nodes.get(parentId)?.session : undefined;
    }
  }

  const roots: SessionTreeNode[] = [];
  for (const node of nodes.values()) {
    const parentId = node.session.parent_session_id;
    const parent = parentId ? nodes.get(parentId) : undefined;
    if (parent && parent !== node) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }

  const sortNodes = (items: SessionTreeNode[], ancestors: Set<string>): void => {
    items.sort((left, right) => {
      const leftTime = new Date(left.session.updated_at || left.session.created_at || '').getTime();
      const rightTime = new Date(right.session.updated_at || right.session.created_at || '').getTime();
      return rightTime - leftTime;
    });
    for (const item of items) {
      if (ancestors.has(item.session.session_id)) {
        throw new Error(`会话树存在循环绑定：${item.session.session_id}`);
      }
      sortNodes(item.children, new Set([...ancestors, item.session.session_id]));
    }
  };
  sortNodes(roots, new Set());
  return roots;
}

export default function SessionTree({ sessions, currentSessionId, onSelectSession, onSetSessionParent }: SessionTreeProps) {
  const roots = useMemo(() => buildSessionTree(sessions), [sessions]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<SessionMenuState | null>(null);
  const [menuError, setMenuError] = useState<string | null>(null);
  const [copiedSessionId, setCopiedSessionId] = useState<string | null>(null);
  const [showAllRoots, setShowAllRoots] = useState(false);
  const visibleRoots = showAllRoots ? roots : roots.slice(0, DEFAULT_VISIBLE_ROOT_COUNT);
  const hiddenSessionCount = roots
    .slice(DEFAULT_VISIBLE_ROOT_COUNT)
    .reduce((total, node) => total + countTreeSessions(node), 0);

  useEffect(() => {
    setExpandedIds(previous => {
      const next = new Set(previous);
      for (const session of sessions) {
        if (sessions.some(candidate => candidate.parent_session_id === session.session_id)) {
          next.add(session.session_id);
        }
      }
      return next;
    });
  }, [sessions]);

  useEffect(() => {
    if (!menu) return undefined;
    const closeMenu = () => setMenu(null);
    window.addEventListener('pointerdown', closeMenu);
    return () => window.removeEventListener('pointerdown', closeMenu);
  }, [menu]);

  const setParent = async (sessionId: string, parentSessionId: string | null): Promise<void> => {
    setMenu(null);
    await onSetSessionParent(sessionId, parentSessionId);
  };

  const scheduleSetParent = (sessionId: string, parentSessionId: string | null): void => {
    void setParent(sessionId, parentSessionId).catch((error: unknown) => {
      console.error('更新会话父子关系失败', error);
    });
  };

  const copySessionId = (sessionId: string): void => {
    setCopiedSessionId(sessionId);
    if (!navigator.clipboard?.writeText) {
      setMenu(null);
      return;
    }
    void navigator.clipboard.writeText(sessionId).then(
      () => setMenu(null),
      (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setMenuError(`复制会话 ID 失败: ${message}`);
      },
    );
  };

  const bindClipboardSession = (parentSessionId: string): void => {
    const readSessionId = async (): Promise<string> => {
      if (navigator.clipboard?.readText) {
        try {
          const clipboardSessionId = (await navigator.clipboard.readText()).trim();
          if (clipboardSessionId) return clipboardSessionId;
        } catch (error) {
          if (!copiedSessionId) throw error;
        }
      }
      if (copiedSessionId) return copiedSessionId;
      throw new Error('剪贴板中没有会话 ID，且应用内没有最近复制的会话 ID');
    };
    void readSessionId()
      .then((sessionId) => {
        return setParent(sessionId, parentSessionId);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setMenuError(`绑定剪贴板会话失败: ${message}`);
      });
  };

  const renderNode = (node: SessionTreeNode): JSX.Element => {
    const { session, children } = node;
    const isExpanded = expandedIds.has(session.session_id);
    const isActive = session.session_id === currentSessionId;

    const toggleExpanded = (): void => {
      setExpandedIds(previous => {
        const next = new Set(previous);
        if (next.has(session.session_id)) next.delete(session.session_id);
        else next.add(session.session_id);
        return next;
      });
    };

    const openMenu = (event: MouseEvent<HTMLButtonElement>): void => {
      event.preventDefault();
      setMenuError(null);
      setMenu({
        sessionId: session.session_id,
        parentSessionId: session.parent_session_id ?? null,
        x: event.clientX,
        y: event.clientY,
      });
    };

    return (
      <li className="session-tree-node" key={session.session_id}>
        <div className="session-tree-row">
          <button
            type="button"
            className={`session-item${isActive ? ' active' : ''}`}
            title={session.title || '未命名'}
            onClick={() => onSelectSession(session.session_id)}
            onContextMenu={openMenu}
          >
            <span className="session-title">{session.title || '未命名'}</span>
            <span className="session-time">{formatTime(session.updated_at || session.created_at) || '刚刚'}</span>
            {children.length > 0 ? (
              <span
                className={`codicon codicon-chevron-${isExpanded ? 'down' : 'right'} session-tree-chevron`}
                aria-hidden="true"
                title={isExpanded ? '折叠子会话' : '展开子会话'}
                onClick={event => {
                  event.stopPropagation();
                  toggleExpanded();
                }}
              />
            ) : null}
          </button>
        </div>
        {isExpanded && children.length > 0 ? (
          <div className="session-tree-children">
            {children.length > 0 ? <ul className="session-tree-list">{children.map(renderNode)}</ul> : null}
          </div>
        ) : null}
      </li>
    );
  };

  return (
    <>
      <ul className="session-tree-list">{visibleRoots.map(renderNode)}</ul>
      {roots.length > DEFAULT_VISIBLE_ROOT_COUNT ? (
        <button
          type="button"
          className="session-show-more-button"
          onClick={() => setShowAllRoots(previous => !previous)}
        >
          {showAllRoots ? '收起其余会话' : `显示剩余 ${hiddenSessionCount} 个会话`}
        </button>
      ) : null}
      {menu ? (
        <div className="session-context-menu" role="menu" style={{ left: menu.x, top: menu.y }} onPointerDown={event => event.stopPropagation()}>
          <button type="button" role="menuitem" title="复制当前会话 ID" onClick={() => copySessionId(menu.sessionId)}>
            <span className="codicon codicon-copy session-menu-item-icon" aria-hidden="true" />
            <span className="session-menu-item-label">复制 ID</span>
          </button>
          <button type="button" role="menuitem" title="将剪贴板中的会话 ID 绑定为当前会话的子会话" onClick={() => bindClipboardSession(menu.sessionId)}>
            <span className="codicon codicon-clippy session-menu-item-icon" aria-hidden="true" />
            <span className="session-menu-item-label">粘贴为子会话</span>
          </button>
          {menu.parentSessionId ? (
            <button type="button" role="menuitem" onClick={() => scheduleSetParent(menu.sessionId, null)}>
              <span className="codicon codicon-debug-disconnect session-menu-item-icon" aria-hidden="true" />
              <span className="session-menu-item-label">移出父会话</span>
            </button>
          ) : null}
          {menuError ? <div className="session-context-menu-error">{menuError}</div> : null}
        </div>
      ) : null}
    </>
  );
}
