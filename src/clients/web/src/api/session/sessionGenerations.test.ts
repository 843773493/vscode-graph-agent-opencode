import { afterEach, expect, test } from "bun:test";
import type { SessionGenerationPreviewDTO } from "../../types/backend";
import { previewSessionGeneration } from "./sessionGenerations";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "./sessionApiFetchMock";

afterEach(unwrapSessionApiFetch);

test("命名预览显式路由到所选目标工作区", async () => {
  const payload = { name: "任务", title_template: "{generator.name}", path_template: ["目录"] };
  const preview: SessionGenerationPreviewDTO = {
    workspace_id: "00000000-0000-4000-8000-000000000001", title: "任务", path_segments: ["目录"],
    navigation_path: ["目录", "任务"], physical_location: "unallocated", target_session_id: null,
  };
  installSessionApiFetchMock(({ path, method, init }) => {
    expect(path).toBe("/api/v1/session-generations/preview-placement");
    expect(method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(payload);
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe("gateway-preview");
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe("00000000-0000-4000-8000-000000000001");
    return Response.json({ data: preview, request_id: "preview" });
  });
  expect(await previewSessionGeneration(48401, {
    gateway_id: "gateway-preview", workspace_id: "00000000-0000-4000-8000-000000000001",
  }, payload, new AbortController().signal)).toEqual(preview);
});
