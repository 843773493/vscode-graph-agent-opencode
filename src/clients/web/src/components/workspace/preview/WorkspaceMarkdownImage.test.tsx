import { afterEach, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../../../api";
import WorkspaceMarkdownImage from "./WorkspaceMarkdownImage";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

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

afterEach(() => {
  for (const restore of restoreFns.splice(0)) restore();
});

/**
 * 图片源在一次请求在途期间被替换时，迟到的旧响应不得再创建对象 URL。
 *
 * 与同一目录的 WorkspaceAttachmentPreview 是同型边界：异步读取完成后必须先判定
 * 「本次请求是否已作废」再创建对象 URL。旧实现直接 `URL.createObjectURL(blob)`，
 * 于是被 abort 的旧请求迟到返回时仍会新建一个永远得不到 revoke 的对象 URL，
 * 并把旧图片写进状态，在用户已经切到另一张图后渲染出上一张。
 */
test("在途期间切换图片源后，迟到的旧响应不得创建对象 URL 或回写旧图", async () => {
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
    renderer = create(
      <WorkspaceMarkdownImage
        apiPort={8014}
        workspaceId="gw_1"
        markdownPath="docs/readme.md"
        src="./old.png"
        alt="旧图"
      />,
    );
  });
  await flush();

  // 用户切到另一张图：旧请求仍被 abort 挂在途中。
  act(() => {
    renderer.update(
      <WorkspaceMarkdownImage
        apiPort={8014}
        workspaceId="gw_1"
        markdownPath="docs/readme.md"
        src="./new.png"
        alt="新图"
      />,
    );
  });
  await flush();

  // 迟到的旧响应此刻才返回。
  await act(async () => {
    oldRequest.resolve(new Blob(["old"]));
    await Promise.resolve();
  });
  await flush();
  // 旧响应被作废：不应为它创建对象 URL。
  expect(created).toBe(0);

  await act(async () => {
    newRequest.resolve(new Blob(["new"]));
    await Promise.resolve();
  });
  await flush();
  const image = renderer.root.findByType("img");
  expect(image.props.src).toBe("blob:fake-1");

  act(() => renderer.unmount());
  // 没有对象 URL 泄漏：创建几个就回收几个。
  expect(created).toBe(revoked.length);
});
