import type { ReactNode } from "react";
import { buildSessionTree, type SessionTreeNode } from "../../state/session/sessionTree";
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
  treeId: string;
  collapsedSessionIds: Set<string>;
  showAllRoots: boolean;
  onSelectSession: (sessionId: string) => void;
  onOpenMenu: (session: Session, x: number, y: number) => void;
  onToggleSession: (sessionId: string) => void;
  onToggleShowAllRoots: (treeId: string) => void;
}

export default function AgentSessionsSessionTree({
  sessions,
  sortMode,
  currentSessionId,
  active,
  treeId,
  collapsedSessionIds,
  showAllRoots,
  onSelectSession,
  onOpenMenu,
  onToggleSession,
  onToggleShowAllRoots,
}: AgentSessionsSessionTreeProps): ReactNode {
  const tree = buildSessionTree(sessions, sortMode);
  const visibleRoots = showAllRoots
    ? tree
    : tree.slice(0, DEFAULT_VISIBLE_ROOT_COUNT);
  const hiddenSessionCount = tree
    .slice(DEFAULT_VISIBLE_ROOT_COUNT)
    .reduce((total, node) => total + countTreeSessions(node), 0);

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
            onToggle={() => onToggleSession(sessionId)}
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
          onClick={() => onToggleShowAllRoots(treeId)}
        >
          {showAllRoots ? "收起其余会话" : `显示剩余 ${hiddenSessionCount} 个会话`}
        </button>
      ) : null}
    </>
  );
}
