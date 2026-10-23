import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../../api";
import WorkspaceMarkdownImage from "./WorkspaceMarkdownImage";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

const MARKDOWN_PATH = "/resources/files/ws/docs/readme.md";

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

const restoreFns: Array<() => void> = [];

interface TestIntersectionObserverHandle {
  disconnected: boolean;
  rootMargin: string;
  intersect: () => void;
}

afterEach(() => {
  for (const restore of restoreFns.splice(0)) restore();
});

function installIntersectionObserver(): TestIntersectionObserverHandle[] {
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "IntersectionObserver");
  const observers: TestIntersectionObserver[] = [];

  class TestIntersectionObserver implements IntersectionObserver {
    readonly root: Element | null = null;
    readonly rootMargin: string;
    readonly thresholds: readonly number[];
    readonly callback: IntersectionObserverCallback;
    target: Element | null = null;
    disconnected = false;

    constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
      this.callback = callback;
      this.rootMargin = options?.rootMargin ?? "0px";
      this.thresholds = Array.isArray(options?.threshold)
        ? options.threshold
        : [options?.threshold ?? 0];
      observers.push(this);
    }

    observe(target: Element): void {
      this.target = target;
    }

    unobserve(target: Element): void {
      if (this.target === target) this.target = null;
    }

    disconnect(): void {
      this.disconnected = true;
      this.target = null;
    }

    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }

    intersect(): void {
      if (!this.target) return;
      this.callback([{
        target: this.target,
        isIntersecting: true,
        intersectionRatio: 1,
      } as IntersectionObserverEntry], this);
    }
  }

  Object.defineProperty(globalThis, "IntersectionObserver", {
    configurable: true,
    writable: true,
    value: TestIntersectionObserver,
  });
  restoreFns.push(() => {
    if (originalDescriptor) {
      Object.defineProperty(globalThis, "IntersectionObserver", originalDescriptor);
    } else {
      Reflect.deleteProperty(globalThis, "IntersectionObserver");
    }
  });
  return observers;
}

function markdownImage(src: string, alt: string) {
  return (
    <WorkspaceMarkdownImage
      apiPort={8014}
      workspaceId="gw_1"
      markdownPath={MARKDOWN_PATH}
      src={src}
      alt={alt}
    />
  );
}

/**
 * 图片源在一次请求在途期间被替换时，迟到的旧响应不得再创建对象 URL。
 *
 * 与同一目录的 WorkspaceAttachmentPreview 是同型边界：异步读取完成后必须先判定
 * 「本次请求是否已作废」再创建对象 URL。旧实现直接 `URL.createObjectURL(blob)`，
 * 于是被 abort 的旧请求迟到返回时仍会新建一个永远得不到 revoke 的对象 URL，
 * 并把旧图片写进状态，在用户已经切到另一张图后渲染出上一张。
 */
test("在途期间切换图片源后，迟到的旧响应不得创建对象 URL 或回写旧图", async () => {
  const observers = installIntersectionObserver();
  const oldRequest = deferred<Blob>();
  const newRequest = deferred<Blob>();
  const getBlob = spyOn(api, "getWorkspaceRawFileBlob")
    .mockImplementation(async (_port, path) => (
      path.includes("old.png") ? await oldRequest.promise : await newRequest.promise
    ));
  restoreFns.push(() => getBlob.mockRestore());

  let created = 0;
  const revoked: string[] = [];
  const createOriginal = URL.createObjectURL;
  const revokeOriginal = URL.revokeObjectURL;
  URL.createObjectURL = (() => `blob:fake-${++created}`) as typeof URL.createObjectURL;
  URL.revokeObjectURL = ((url: string) => {
    revoked.push(url);
  }) as typeof URL.revokeObjectURL;
  restoreFns.push(() => {
    URL.createObjectURL = createOriginal;
    URL.revokeObjectURL = revokeOriginal;
  });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(markdownImage("./old.png", "旧图"), {
      createNodeMock: (element) => element.type === "span"
        ? {} as HTMLSpanElement
        : null,
    });
  });
  await flush();
  expect(getBlob).not.toHaveBeenCalled();
  expect(observers).toHaveLength(1);
  expect(observers[0]!.rootMargin).toBe("200px 0px");

  await act(async () => {
    observers[0]!.intersect();
    await Promise.resolve();
  });
  await flush();
  expect(getBlob).toHaveBeenCalledTimes(1);
  expect(getBlob.mock.calls[0]?.[1]).toBe("/resources/files/ws/docs/old.png");

  // 用户切到另一张图：旧请求仍被 abort 挂在途中。
  act(() => {
    renderer.update(markdownImage("./new.png", "新图"));
  });
  await flush();
  expect(observers).toHaveLength(2);
  expect(observers[0]!.disconnected).toBe(true);
  expect(getBlob).toHaveBeenCalledTimes(1);

  // 迟到的旧响应此刻才返回。
  await act(async () => {
    oldRequest.resolve(new Blob(["old"]));
    await Promise.resolve();
  });
  await flush();
  // 旧响应被作废：不应为它创建对象 URL。
  expect(created).toBe(0);

  await act(async () => {
    observers[1]!.intersect();
    await Promise.resolve();
  });
  await flush();
  expect(getBlob).toHaveBeenCalledTimes(2);
  expect(getBlob.mock.calls[1]?.[1]).toBe("/resources/files/ws/docs/new.png");

  await act(async () => {
    newRequest.resolve(new Blob(["new"]));
    await Promise.resolve();
  });
  await flush();
  const image = renderer.root.findByType("img");
  expect(image.props.src).toBe("blob:fake-1");

  act(() => renderer.unmount());
  expect(observers[1]!.disconnected).toBe(true);
  // 没有对象 URL 泄漏：创建几个就回收几个。
  expect(created).toBe(revoked.length);
});

test("外部 Markdown 图片使用浏览器原生懒加载，不请求工作区文件", () => {
  const getBlob = spyOn(api, "getWorkspaceRawFileBlob");
  restoreFns.push(() => getBlob.mockRestore());

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(markdownImage("https://example.com/image.png", "外部图片"));
  });

  const image = renderer.root.findByType("img");
  expect(image.props.src).toBe("https://example.com/image.png");
  expect(image.props.loading).toBe("lazy");
  expect(getBlob).not.toHaveBeenCalled();
  act(() => renderer.unmount());
});
