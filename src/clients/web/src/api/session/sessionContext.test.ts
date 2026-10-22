import { afterEach, expect, test } from "bun:test";
import {
  installSessionApiFetchMock,
  unwrapSessionApiFetch,
} from "./sessionApiFetchMock";
import { readSessionContext, type SessionContextSession } from "./sessionContext";

afterEach(() => {
  unwrapSessionApiFetch();
});

test("上下文请求透传 Session API 身份与目录返回的 assembly_ref", async () => {
  const session: SessionContextSession = {
    session_id: "ses_context",
    resource_identity: "0c06a4d2-9bd5-5b60-8058-d75ad747331d",
    uri: "boxteam://workspace/11111111-1111-4111-8111-111111111111/resources/session/ses_context",
    scope: "workspace",
  };
  let requestBody: unknown;
  installSessionApiFetchMock(({ path, method, init }) => {
    if (path !== "/api/v1/context/read" || method !== "POST") return undefined;
    requestBody = JSON.parse(String(init?.body));
    return Response.json({
      code: 0,
      message: "ok",
      request_id: "req-context",
      data: {
        ...session,
        view: "assembly",
        assembly_ref: "assembly-ref-from-catalog",
        revision: "revision-context",
        items: [],
        partial_errors: [],
      },
    });
  });

  const result = await readSessionContext(
    49_321,
    "gateway-workspace",
    session,
    "assembly",
    {
      signal: new AbortController().signal,
      assemblyRef: "assembly-ref-from-catalog",
    },
  );

  expect(requestBody).toMatchObject({
    resource_identity: session.resource_identity,
    uri: session.uri,
    scope: session.scope,
    assembly_ref: "assembly-ref-from-catalog",
  });
  expect(requestBody).not.toHaveProperty("resource");
  expect(result.assembly_ref).toBe("assembly-ref-from-catalog");
});
