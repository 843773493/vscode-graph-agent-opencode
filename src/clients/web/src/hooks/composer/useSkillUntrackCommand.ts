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
  const activeScopeKeyRef = useRef(scopeKey);
  activeScopeKeyRef.current = scopeKey;

  return useCallback((args: string) => {
    const command = async () => {
      const skillArgumentsMatch = /^("(?:[^"\\]|\\.)*"|[^\s"]+)(?:\s+(\S+))?$/u.exec(args.trim());
      if (!skillArgumentsMatch) throw new Error('用法：/skill-untrack <Skill 名称> [线程 ID]；名称含空格时使用双引号');
      const skillName = (skillArgumentsMatch[1].startsWith('"')
        ? JSON.parse(skillArgumentsMatch[1]) as string
        : skillArgumentsMatch[1]).trim();
      if (!skillName) throw new Error("Skill 名称不能为空");
      if (!sessionId || !workspaceId || state.apiPort === null) throw new Error("请先选择会话");
      // 首个 await 前复制完整路由，后续切换或更新登记不能改变这次控制操作的归属。
      const gatewayWorkspaceTarget = {
        ...requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, workspaceId),
      };
      const apiPort = state.apiPort;
      const threadId = skillArgumentsMatch[2]
        ?? (await getSessionMainThread(apiPort, sessionId, gatewayWorkspaceTarget)).thread_id;
      const untrackResult = await untrackSessionSkill(
        apiPort,
        gatewayWorkspaceTarget,
        sessionId,
        threadId,
        skillName,
      );
      if (untrackResult.status === "error") {
        throw new Error(untrackResult.error?.message ?? "后端返回停止跟踪失败");
      }
      if (!["loaded", "not_tracked"].includes(untrackResult.status) || untrackResult.error !== null
        || untrackResult.append_status !== "none" || untrackResult.tracked !== false || untrackResult.queued !== false) {
        throw new Error("停止跟踪尚未完成，请重新检查后端状态");
      }
      if (activeScopeKeyRef.current === scopeKey) {
        setNotice(untrackResult.status === "not_tracked"
          ? `${untrackResult.name} 在该线程未被跟踪，已有上下文保留`
          : `已停止跟踪 ${untrackResult.name}，已有上下文保留`);
      }
    };
    return command().catch((error: unknown) => {
      if (activeScopeKeyRef.current === scopeKey) setError(`停止跟踪 Skill 失败：${errorMessage(error)}`);
    });
  }, [state.apiPort, state.gatewayWorkspaces, sessionId, workspaceId, scopeKey, setNotice, setError]);
}
