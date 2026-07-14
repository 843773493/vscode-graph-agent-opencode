import { buildSessionTree } from "../sessionTree";
import type { Session } from "../../types/backend";

function session(
  sessionId: string,
  parentSessionId: string | null,
  updatedAt: string,
): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
    title: sessionId,
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: parentSessionId,
    created_at: updatedAt,
    updated_at: updatedAt,
  };
}

const tree = buildSessionTree([
  session("child", "root", "2026-01-02T00:00:00Z"),
  session("root", null, "2026-01-01T00:00:00Z"),
  session("grandchild", "child", "2026-01-03T00:00:00Z"),
  session("orphan", "missing", "2026-01-04T00:00:00Z"),
]);

if (tree.map((node) => node.session.session_id).join(",") !== "orphan,root") {
  throw new Error("根会话排序或缺失父会话提升规则错误");
}
if (tree[1]?.children[0]?.session.session_id !== "child") {
  throw new Error("子会话未绑定到父节点");
}
if (tree[1]?.children[0]?.children[0]?.session.session_id !== "grandchild") {
  throw new Error("多层子会话树构建失败");
}

let cycleRejected = false;
try {
  buildSessionTree([
    session("a", "b", "2026-01-01T00:00:00Z"),
    session("b", "a", "2026-01-02T00:00:00Z"),
  ]);
} catch (error) {
  cycleRejected = error instanceof Error && error.message.includes("循环");
}
if (!cycleRejected) {
  throw new Error("循环会话树未快速失败");
}
