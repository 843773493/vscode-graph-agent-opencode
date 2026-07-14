import { useState, type ReactNode } from "react";
import { buildSessionTree, type SessionTreeNode } from "../../state/sessionTree";
import type { Session } from "../../types/backend";
import AgentSessionsSessionButton from "./AgentSessionsSessionButton";

const DEFAULT_VISIBLE_ROOT_COUNT = 5;

function countTreeSessions(node: SessionTreeNode): number {
  return 1 + node.children.reduce(
    (total, child) => total + countTreeSessions(child),
    0,
  );
}

interface AgentSessionsSessionTreeProps {
  sessions: Session[];
  sortMode: "created" | "updated";
  currentSessionId: string;
  active: boolean;
  onSelectSession: (sessionId: string) => void;
  onOpenMenu: (session: Session, x: number, y: number) => void;
}

export default function AgentSessionsSessionTree({
  sessions,
  sortMode,
  currentSessionId,
  active,
  onSelectSession,
  onOpenMenu,
}: AgentSessionsSessionTreeProps): ReactNode {
  const [collapsedSessionIds, setCollapsedSessionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [showAllRoots, setShowAllRoots] = useState(false);
  const tree = buildSessionTree(sessions, sortMode);
  const visibleRoots = showAllRoots
    ? tree
    : tree.slice(0, DEFAULT_VISIBLE_ROOT_COUNT);
  const hiddenSessionCount = tree
    .slice(DEFAULT_VISIBLE_ROOT_COUNT)
    .reduce((total, node) => total + countTreeSessions(node), 0);

  const toggleNode = (node: SessionTreeNode) => {
    setCollapsedSessionIds((prev) => {
      const next = new Set(prev);
      if (next.has(node.session.session_id)) {
        next.delete(node.session.session_id);
      } else {
        next.add(node.session.session_id);
      }
      return next;
    });
  };

  const renderNode = (node: SessionTreeNode): ReactNode => {
    const sessionId = node.session.session_id;
    const expanded =
      node.children.length > 0 && !collapsedSessionIds.has(sessionId);
    return (
      <li className="session-tree-node" key={sessionId}>
        <div className="session-tree-row">
          <AgentSessionsSessionButton
            session={node.session}
            isActive={active && sessionId === currentSessionId}
            onSelectSession={onSelectSession}
            onOpenMenu={onOpenMenu}
            expanded={expanded}
            showToggle={node.children.length > 0}
            onToggle={() => toggleNode(node)}
          />
        </div>
        {expanded && node.children.length > 0 ? (
          <ul className="session-tree-children">
            {node.children.map(renderNode)}
          </ul>
        ) : null}
      </li>
    );
  };

  return (
    <>
      <ul className="session-list session-tree">{visibleRoots.map(renderNode)}</ul>
      {tree.length > DEFAULT_VISIBLE_ROOT_COUNT ? (
        <button
          type="button"
          className="session-show-more-button"
          onClick={() => setShowAllRoots((previous) => !previous)}
        >
          {showAllRoots ? "收起其余会话" : `显示剩余 ${hiddenSessionCount} 个会话`}
        </button>
      ) : null}
    </>
  );
}
