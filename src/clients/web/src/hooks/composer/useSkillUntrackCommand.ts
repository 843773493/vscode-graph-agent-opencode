import { t } from "../../../../shared/core/i18n/index.js";
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
      if (!skillArgumentsMatch) throw new Error(t("slash.untrackUsage"));
      const skillName = (skillArgumentsMatch[1].startsWith('"')
        ? JSON.parse(skillArgumentsMatch[1]) as string
        : skillArgumentsMatch[1]).trim();
      if (!skillName) throw new Error(t("slash.skillRequired"));
      if (!sessionId || !workspaceId || state.apiPort === null) throw new Error(t("slash.sessionRequired"));
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
        throw new Error(untrackResult.error?.message ?? t("slash.untrackBackendFailed"));
      }
      if (!["loaded", "not_tracked"].includes(untrackResult.status) || untrackResult.error !== null
        || untrackResult.append_status !== "none" || untrackResult.tracked !== false || untrackResult.queued !== false) {
        throw new Error(t("slash.untrackIncomplete"));
      }
      if (activeScopeKeyRef.current === scopeKey) {
        setNotice(untrackResult.status === "not_tracked"
          ? t("slash.skillNotTracked", untrackResult.name)
          : t("slash.skillUntracked", untrackResult.name));
      }
    };
    return command().catch((error: unknown) => {
      if (activeScopeKeyRef.current === scopeKey) setError(t("slash.untrackFailed", errorMessage(error)));
    });
  }, [state.apiPort, state.gatewayWorkspaces, sessionId, workspaceId, scopeKey, setNotice, setError]);
}
