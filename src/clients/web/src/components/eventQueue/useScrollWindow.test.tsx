import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useScrollWindow } from "./useScrollWindow";

interface FakeList {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}

interface WindowOptions {
  active: boolean;
  sessionId: string;
  itemCount: number;
  initialVisibleCount: number;
  olderBatchSize: number;
  onExhaustedOlderItems?: () => void;
}

/**
 * 直接挂载共享滚动窗口 hook，并用受控的假列表元素驱动滚动路径；
 * 组件级测试走 renderToStaticMarkup，不会触发 onScroll，因此这里补上该覆盖。
 */
function mountWindow(initial: WindowOptions) {
  let options: WindowOptions = { ...initial };
  let latest: ReturnType<typeof useScrollWindow> | undefined;
  let renderer: ReactTestRenderer | undefined;

  function Probe(): React.ReactNode {
    latest = useScrollWindow(options);
    return null;
  }

  act(() => {
    renderer = create(<Probe />);
  });

  const attachList = (list: FakeList) => {
    (latest!.listRef as unknown as { current: FakeList }).current = list;
  };

  return {
    hook: () => latest!,
    attachList,
    /** 状态更新必须包在 act 内，否则滚动恢复副作用不会冲刷。 */
    run(action: () => void) {
      act(action);
    },
    update(patch: Partial<WindowOptions>) {
      options = { ...options, ...patch };
      act(() => renderer!.update(<Probe />));
    },
    unmount() {
      act(() => renderer!.unmount());
    },
  };
}

describe("共享滚动窗口", () => {
  test("向前展开受条目总数约束，新增条目后仍保留未展示的尾部", () => {
    const windowed = mountWindow({
      active: true,
      sessionId: "ses_window",
      itemCount: 50,
      initialVisibleCount: 10,
      olderBatchSize: 30,
    });
    windowed.attachList({ scrollHeight: 100, scrollTop: 50, clientHeight: 50 });

    // 初始窗口：50 - 10 = 40 条待向前展开。
    expect(windowed.hook().firstVisibleIndex).toBe(40);

    expect(windowed.hook().revealOlderItems()).toBe(true);
    expect(windowed.hook().firstVisibleIndex).toBe(10);

    // 再展开一批：窗口被 Math.min 收敛到恰好等于条目总数，而不是越过它。
    expect(windowed.hook().revealOlderItems()).toBe(true);
    expect(windowed.hook().firstVisibleIndex).toBe(0);

    // 条目总数增长后，收敛过的窗口应仍然只展示新增的一部分；
    // 若窗口越过条目总数，这里会变成 0（整段一次性铺开）。
    windowed.update({ itemCount: 60 });
    expect(windowed.hook().firstVisibleIndex).toBe(10);

    windowed.unmount();
  });

  test("滚动锚点按高度差恢复，丢弃锚点后回到贴底", () => {
    const windowed = mountWindow({
      active: true,
      sessionId: "ses_window",
      itemCount: 60,
      initialVisibleCount: 10,
      olderBatchSize: 10,
    });
    const list: FakeList = { scrollHeight: 200, scrollTop: 50, clientHeight: 50 };
    windowed.attachList(list);

    // 记录锚点后内容变高：scrollTop 应保持「距底部偏移不变」。
    const ticket = windowed.hook().beginOlderLoad();
    windowed.hook().captureScrollAnchor();
    list.scrollHeight = 400;
    windowed.run(() => windowed.hook().appendVisibleCount(10, ticket));
    expect(list.scrollTop).toBe(250);

    // 丢弃锚点后不再做高度差恢复，而是走贴底逻辑。
    windowed.hook().captureScrollAnchor();
    windowed.hook().discardScrollAnchor(windowed.hook().beginOlderLoad());
    list.scrollHeight = 600;
    windowed.run(() =>
      windowed.hook().appendVisibleCount(10, windowed.hook().beginOlderLoad()));
    expect(list.scrollTop).toBe(600);

    windowed.unmount();
  });

  test("可见窗口用尽时向上滚动触发服务端旧页回调", () => {
    let exhausted = 0;
    const windowed = mountWindow({
      active: true,
      sessionId: "ses_window",
      itemCount: 5,
      initialVisibleCount: 10,
      olderBatchSize: 10,
      onExhaustedOlderItems: () => { exhausted += 1; },
    });
    const list: FakeList = { scrollHeight: 100, scrollTop: 0, clientHeight: 100 };
    windowed.attachList(list);

    // 条目总数少于初始窗口：firstVisibleIndex 为 0，没有可展开的客户端窗口。
    expect(windowed.hook().firstVisibleIndex).toBe(0);
    windowed.hook().handleListScroll();
    expect(exhausted).toBe(1);

    // 已贴近底部时不应请求旧页。
    list.scrollTop = 90;
    windowed.hook().handleListScroll();
    expect(exhausted).toBe(1);

    windowed.unmount();
  });

  test("会话切换后迟到的旧页结果不累加到新会话窗口", () => {
    const windowed = mountWindow({
      active: true,
      sessionId: "ses_old",
      itemCount: 100,
      initialVisibleCount: 10,
      olderBatchSize: 10,
    });
    windowed.attachList({ scrollHeight: 100, scrollTop: 50, clientHeight: 50 });

    // 旧会话发起一次旧页请求并拿到凭证，请求在途时切到新会话。
    const staleTicket = windowed.hook().beginOlderLoad();
    windowed.update({ sessionId: "ses_new", itemCount: 20 });

    // 新会话窗口按 initialVisibleCount 重置：20 - 10 = 10。
    expect(windowed.hook().firstVisibleIndex).toBe(10);

    // 旧会话的响应此时才到达：必须被归属守卫丢弃，不能把旧页条数算进来。
    windowed.run(() => windowed.hook().appendVisibleCount(40, staleTicket));
    expect(windowed.hook().firstVisibleIndex).toBe(10);

    // 当前窗口自己的凭证仍然有效，累加照常生效。
    windowed.run(() =>
      windowed.hook().appendVisibleCount(10, windowed.hook().beginOlderLoad()));
    expect(windowed.hook().firstVisibleIndex).toBe(0);

    windowed.unmount();
  });
});
