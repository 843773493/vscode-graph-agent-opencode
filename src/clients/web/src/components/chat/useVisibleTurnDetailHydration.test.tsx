import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { ConversationView } from "../../types/frontend";
import { useVisibleTurnDetailHydration } from "./useVisibleTurnDetailHydration";

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
  reject: (error: Error) => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => undefined;
  let reject: (error: Error) => void = () => undefined;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function summaryConversation(sessionId: string): ConversationView {
  return {
    conversationId: `${sessionId}:job_same`,
    displayMode: "history",
    turnId: "job_same",
    turnRevision: 1,
    turnItemsView: "summary",
    sessionId,
    userMessage: null,
    events: [],
    status: "done",
    jobId: "job_same",
    pending: false,
    source: "turn",
  };
}

describe("useVisibleTurnDetailHydration 会话作用域", () => {
  test("旧请求失败不污染新会话，也不删除新会话同名 Turn 的 pending 标记", async () => {
    const firstRequest = deferred();
    const secondRequest = deferred();
    const requests: string[][] = [];
    let requestIndex = 0;
    let hydration: ReturnType<typeof useVisibleTurnDetailHydration> | null = null;
    const loadTurnDetails = (turnIds: string[]): Promise<void> => {
      requests.push(turnIds);
      requestIndex += 1;
      if (requestIndex === 1) return firstRequest.promise;
      if (requestIndex === 2) return secondRequest.promise;
      throw new Error("pending 标记被意外清除，触发了重复详情请求");
    };

    function Harness({ sessionId }: { sessionId: string }): React.ReactNode {
      hydration = useVisibleTurnDetailHydration({
        sessionId,
        timelineGeneration: 1,
        projectionEpoch: 1,
        conversations: [summaryConversation(sessionId)],
        firstItemIndex: 99_999,
        loadingTurnIds: [],
        onLoadTurnDetails: loadTurnDetails,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<Harness sessionId="session-a" />);
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });
    act(() => {
      renderer!.update(<Harness sessionId="session-b" />);
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });

    await act(async () => {
      firstRequest.reject(new Error("旧会话详情失败"));
      await Promise.resolve();
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });

    expect(requests).toEqual([["job_same"], ["job_same"]]);
    expect(hydration!.detailHydrationError).toBe(null);

    await act(async () => {
      secondRequest.resolve();
      await Promise.resolve();
    });
    renderer!.unmount();
  });

  test("同会话 generation 切换会隔离悬挂旧请求并允许新投影水合", async () => {
    const firstRequest = deferred();
    const secondRequest = deferred();
    const requests: string[][] = [];
    let hydration: ReturnType<typeof useVisibleTurnDetailHydration> | null = null;
    const loadTurnDetails = (turnIds: string[]): Promise<void> => {
      requests.push(turnIds);
      return requests.length === 1 ? firstRequest.promise : secondRequest.promise;
    };

    function Harness({ generation }: { generation: number }): React.ReactNode {
      hydration = useVisibleTurnDetailHydration({
        sessionId: "session-same",
        timelineGeneration: generation,
        projectionEpoch: generation,
        conversations: [summaryConversation("session-same")],
        firstItemIndex: 99_999,
        loadingTurnIds: [],
        onLoadTurnDetails: loadTurnDetails,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<Harness generation={1} />);
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });
    act(() => {
      renderer!.update(<Harness generation={2} />);
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });
    await act(async () => {
      firstRequest.resolve();
      await Promise.resolve();
    });
    act(() => {
      hydration!.hydrateVisibleTurns({ startIndex: 99_999, endIndex: 99_999 });
    });

    expect(requests).toEqual([["job_same"], ["job_same"]]);
    expect(hydration!.detailHydrationError).toBe(null);
    await act(async () => {
      secondRequest.resolve();
      await Promise.resolve();
    });
    renderer!.unmount();
  });
});
