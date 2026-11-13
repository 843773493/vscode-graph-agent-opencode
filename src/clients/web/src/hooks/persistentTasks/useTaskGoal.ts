import { t } from "../../../../shared/core/i18n/index.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createSerialOperationQueue, createSnapshotReadCoordinator, SnapshotReadSupersededError } from "../../../../shared/core/syncState";
import { clearSessionGoal, getSessionGoal, getSessionGoalOperation, updateSessionGoal } from "../../api/session/sessionGoals";
import type { SessionGoalOperationResultDTO, SessionGoalSnapshotDTO, SessionGoalUpdateRequest } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";

/** 详情只镜像原 Goal；读取可撤销，同一 owner 的控制操作串行。 */
export function useTaskGoal(port: number, workspaceId: string, sessionId: string, revision?: number) {
  const [snapshot, setSnapshot] = useState<SessionGoalSnapshotDTO | null>(null);
  const [reading, setReading] = useState(true);
  const [writing, setWriting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reads = useRef(createSnapshotReadCoordinator());
  const writes = useRef(createSerialOperationQueue());
  const key = JSON.stringify([port, workspaceId, sessionId]);
  const scope = useMemo(() => ({ released: false, snapshot: null as SessionGoalSnapshotDTO | null }), [key]);
  const owner = useRef(scope);
  owner.current = scope;
  const valid = useCallback(() => owner.current === scope && !scope.released, [scope]);
  const apply = useCallback((incoming: SessionGoalSnapshotDTO) => {
    if (!valid() || incoming.sync_version < (scope.snapshot?.sync_version ?? 0)) return;
    scope.snapshot = incoming;
    setSnapshot((previous) => valid() ? incoming : previous);
  }, [scope, valid]);

  const refresh = useCallback(async () => {
    if (!valid()) return null;
    setReading(true);
    try {
      const { value, signal } = await reads.current.read(key, async (signal) => ({
        value: await getSessionGoal(port, sessionId, workspaceId, signal, scope.snapshot?.sync_version ?? 0), signal,
      }));
      if (signal.aborted || !valid()) return null;
      apply(value);
      setError(null);
      setReading(false);
      return value.goal ?? null;
    } catch (cause) {
      if (cause instanceof SnapshotReadSupersededError || !valid()) return null;
      setError(errorMessage(cause));
      setReading(false);
      throw cause;
    }
  }, [apply, key, port, workspaceId, sessionId, scope, valid]);

  useEffect(() => {
    scope.released = false;
    setSnapshot(null);
    setWriting(false);
    setError(null);
    return () => { scope.released = true; reads.current.invalidate(key); };
  }, [key, scope]);
  useEffect(() => {
    void refresh().catch(() => { /* 原错误已呈现在详情。 */ });
  }, [refresh, revision]);

  const mutate = (write: (expected: number, operationId: string) => Promise<SessionGoalOperationResultDTO>,
    expected?: number): Promise<SessionGoalOperationResultDTO> => writes.current.run(key, async () => {
    if (!valid()) throw new SnapshotReadSupersededError();
    reads.current.invalidate(key);
    setReading(false);
    setWriting(true);
    setError(null);
    const operationId = crypto.randomUUID();
    let failure: unknown;
    try {
      let result: SessionGoalOperationResultDTO;
      try {
        result = await write(expected ?? scope.snapshot?.control_revision ?? 0, operationId);
      } catch (operationError) {
        let receipt: SessionGoalOperationResultDTO;
        try { receipt = await getSessionGoalOperation(port, sessionId, operationId, workspaceId); }
        catch (queryError) {
          throw new Error(`${errorMessage(operationError)}；查询 Goal 原操作回执失败：${errorMessage(queryError)}`);
        }
        if (receipt.status === "unknown") throw operationError;
        result = receipt;
      }
      if (result.status !== "applied" && result.status !== "already_missing") throw new Error(`Goal 操作未应用: ${result.status}`);
      reads.current.invalidate(key);
      apply(result);
      return result;
    } catch (cause) {
      failure = cause;
      throw cause;
    } finally {
      try { await refresh(); }
      catch (cause) {
        if (failure) throw new Error(t("tasks.goal.reloadFailed", errorMessage(failure), errorMessage(cause)));
        throw cause;
      } finally {
        if (valid()) {
          setWriting(false);
          if (failure) setError(errorMessage(failure));
        }
      }
    }
  });

  return { goal: snapshot?.goal ?? null, controlRevision: snapshot?.control_revision ?? 0, loading: reading || writing, error, refresh,
    update: async (payload: SessionGoalUpdateRequest) => {
      const frozen = { ...payload };
      const result = await mutate((expected, operationId) => updateSessionGoal(port, sessionId, {
        ...frozen, operation_id: operationId, expected_control_revision: expected,
      }, workspaceId), frozen.expected_control_revision);
      if (!result.goal) throw new Error(`Goal 设置未返回已应用目标: ${result.status}`);
      return result.goal;
    },
    clear: async (expectedControlRevision?: number) => {
      await mutate((expected, operationId) => clearSessionGoal(port, sessionId, {
        operation_id: operationId, expected_control_revision: expected,
      }, workspaceId), expectedControlRevision);
    },
  };
}
