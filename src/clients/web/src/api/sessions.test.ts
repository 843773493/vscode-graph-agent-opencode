import { afterEach, describe, expect, test } from "bun:test";
import { HttpRequestError } from "./http";
import { listChildThreads } from "./sessions";

const originalFetch = globalThis.fetch;

/** mock fetch：第一次返回 Gateway 本地凭据，之后返回给定的 API 响应。 */
function installFetchMock(
  respond: (init: RequestInit | undefined, url: string) => Response,
): void {
  let count = 0;
  globalThis.fetch = Object.assign(
    async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
      count += 1;
      const url = typeof input === "string" ? input : input.toString();
      if (count === 1) {
        return Response.json({ data: { token: "child-threads-token" } });
      }
      return respond(init, url);
    },
    { preconnect: originalFetch.preconnect },
  );
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("child thread 列表 API", () => {
  test("成功：返回 ChildThreadList 并携带工作区头与编码后的会话 id", async () => {
    let requestInit: RequestInit | undefined;
    let requestUrl = "";
    installFetchMock((init, url) => {
      requestInit = init;
      requestUrl = url;
      return Response.json({
        data: {
          parent_session_id: "ses_parent",
          items: [
            {
              thread_id: "thr_child_1",
              delegation_id: "del_child_1",
              title: "委派：修复构建",
              created_at: "2026-09-15T12:00:00Z",
              collaboration_state: "published",
              admission_state: "bound",
              status: "running",
              subagent_type: "general-purpose",
            },
          ],
          total: 1,
        },
        request_id: "req-child-threads",
      });
    });

    const list = await listChildThreads(48_301, "ses/needs encoding", "workspace-1");
    expect(list.parent_session_id).toBe("ses_parent");
    expect(list.total).toBe(1);
    expect(list.items[0]?.thread_id).toBe("thr_child_1");
    expect(list.items[0]?.admission_state).toBe("bound");
    expect(list.items[0]?.status).toBe("running");
    expect(requestUrl).toContain(
      "/api/v1/sessions/ses%2Fneeds%20encoding/child-threads",
    );
    expect(new Headers(requestInit?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("workspace-1");
  });

  test("协议状态未知时直接失败", async () => {
    installFetchMock(() => Response.json({
      data: {
        parent_session_id: "ses_parent",
        items: [{
          thread_id: "thr_child_1",
          created_at: "2026-09-15T12:00:00Z",
          status: "stalled",
        }],
        total: 1,
      },
      request_id: "req-child-threads-invalid-status",
    }));

    await expect(
      listChildThreads(48_304, "ses_parent", "workspace-1"),
    ).rejects.toThrow("child thread status 协议值无效");
  });

  test("404：父会话不存在时透明抛出 HttpRequestError", async () => {
    installFetchMock(() => Response.json(
      { detail: "父会话不存在: ses_missing" },
      { status: 404, statusText: "Not Found" },
    ));

    try {
      await listChildThreads(48_302, "ses_missing", "workspace-1");
      throw new Error("listChildThreads 应当抛出错误");
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(HttpRequestError);
      expect((error as HttpRequestError).status).toBe(404);
      expect((error as Error).message).toContain("父会话不存在");
    }
  });

  test("409：目录树异常时透明抛出 HttpRequestError", async () => {
    installFetchMock(() => Response.json(
      { detail: "会话目录索引异常" },
      { status: 409, statusText: "Conflict" },
    ));

    try {
      await listChildThreads(48_303, "ses_parent", "workspace-1");
      throw new Error("listChildThreads 应当抛出错误");
    } catch (error: unknown) {
      expect(error).toBeInstanceOf(HttpRequestError);
      expect((error as HttpRequestError).status).toBe(409);
    }
  });
});
