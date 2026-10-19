import { useCallback, useRef } from "react";
import { getSessionMainThread } from "../../api/session/sessions";
import { untrackSessionSkill } from "../../api/session/sessionSkillTracking";
import { requireGatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import type { ComposerStateSnapshot } from "../../state/composerState";
import { errorMessage } from "../../utils/errorMessage";

export function useSkillUntrackCommand({
  state,
  sessionId,
  workspaceId,
  scopeKey,
  setNotice,
  setError,
}: {
  state: ComposerStateSnapshot;
  sessionId: string | null;
  workspaceId: string | null;
  scopeKey: string | null;
  setNotice: (message: string) => void;
  setError: (message: string) => void;
}) {
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;

  return useCallback((args: string) => {
    const command = async () => {
      const parsed = /^("(?:[^"\\]|\\.)*"|[^\s"]+)(?:\s+(\S+))?$/u.exec(args.trim());
      if (!parsed) throw new Error('用法：/skill-untrack <Skill 名称> [线程 ID]；名称含空格时使用双引号');
      const name = (parsed[1].startsWith('"') ? JSON.parse(parsed[1]) as string : parsed[1]).trim();
      if (!name) throw new Error("Skill 名称不能为空");
      if (!sessionId || !workspaceId || state.apiPort === null) throw new Error("请先选择会话");
      // 首个 await 前复制完整路由，后续切换或更新登记不能改变这次控制操作的归属。
      const target = { ...requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, workspaceId) };
      const port = state.apiPort;
      const threadId = parsed[2] ?? (await getSessionMainThread(port, sessionId, target)).thread_id;
      const result = await untrackSessionSkill(port, target, sessionId, threadId, name);
      if (result.status === "error") throw new Error(result.error?.message ?? "后端返回停止跟踪失败");
      if (!["loaded", "not_tracked"].includes(result.status) || result.error !== null
        || result.append_status !== "none" || result.tracked !== false || result.queued !== false) {
        throw new Error("停止跟踪尚未完成，请重新检查后端状态");
      }
      if (currentScope.current === scopeKey) {
        setNotice(result.status === "not_tracked"
          ? `${result.name} 在该线程未被跟踪，已有上下文保留`
          : `已停止跟踪 ${result.name}，已有上下文保留`);
      }
    };
    return command().catch((error: unknown) => {
      if (currentScope.current === scopeKey) setError(`停止跟踪 Skill 失败：${errorMessage(error)}`);
    });
  }, [state.apiPort, state.gatewayWorkspaces, sessionId, workspaceId, scopeKey, setNotice, setError]);
}
