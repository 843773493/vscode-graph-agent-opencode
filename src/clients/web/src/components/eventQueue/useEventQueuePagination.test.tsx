import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useEventQueuePagination } from "./useEventQueuePagination";

interface FakeList {
  scrollHeight: number;
  scrollTop: number;
}

interface PaginationOptions {
  active: boolean;
  scopeKey: string;
}

interface DeferredNumber {
  promise: Promise<number>;
  resolve: (value: number) => void;
}

function deferredNumber(): DeferredNumber {
  let resolve!: (value: number) => void;
  const promise = new Promise<number>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function mountPagination(initialScopeKey: string) {
  let options: PaginationOptions = { active: true, scopeKey: initialScopeKey };
  let latest: ReturnType<typeof useEventQueuePagination> | undefined;
  let renderer: ReactTestRenderer | undefined;
  const requests: DeferredNumber[] = [];
  const onLoadOlderHistory = () => {
    const request = deferredNumber();
    requests.push(request);
    return request.promise;
  };

  function Probe(): React.ReactNode {
    latest = useEventQueuePagination({
      ...options,
      displayItemCount: 1,
      historyLoading: false,
      historyLoadingOlder: false,
      historyHasMore: true,
      onLoadOlderHistory,
    });
    return null;
  }

  act(() => {
    renderer = create(<Probe />);
  });

  return {
    hook: () => latest!,
    requests,
    attachList(list: FakeList) {
      (latest!.listRef as unknown as { current: FakeList | null }).current = list;
    },
    updateScope(scopeKey: string) {
      options = { ...options, scopeKey };
      act(() => renderer!.update(<Probe />));
    },
    unmount() {
      act(() => renderer!.unmount());
    },
  };
}

async function resolveRequest(request: DeferredNumber): Promise<void> {
  await act(async () => {
    request.resolve(0);
    await request.promise;
  });
}

describe("事件历史分页锁", () => {
  test("A→B→A 允许新作用域加载，迟到请求不能释放当前锁", async () => {
    const pagination = mountPagination("workspace-a:session-shared");
    pagination.attachList({ scrollHeight: 100, scrollTop: 20 });

    act(() => pagination.hook().revealOlderEvents());
    expect(pagination.requests).toHaveLength(1);

    pagination.updateScope("workspace-b:session-shared");
    act(() => pagination.hook().revealOlderEvents());
    expect(pagination.requests).toHaveLength(2);

    pagination.updateScope("workspace-a:session-shared");
    act(() => pagination.hook().revealOlderEvents());
    expect(pagination.requests).toHaveLength(3);

    await resolveRequest(pagination.requests[0]);
    await resolveRequest(pagination.requests[1]);
    act(() => pagination.hook().revealOlderEvents());
    expect(pagination.requests).toHaveLength(3);

    await resolveRequest(pagination.requests[2]);
    act(() => pagination.hook().revealOlderEvents());
    expect(pagination.requests).toHaveLength(4);

    await resolveRequest(pagination.requests[3]);
    pagination.unmount();
  });
});
