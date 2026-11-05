import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installGatewayFetch, restoreGatewayTestEnvironment } from "../../../tests/fixtures/session/gatewayEnvironment";
import type { GatewayDiagnosticLogLocateRequest } from "../../../types/protocol_generated/boxteam/gateway/v2/public";
import GatewayDiagnosticLogLocation from "./GatewayDiagnosticLogLocation";

const request = (start = 2): GatewayDiagnosticLogLocateRequest => ({ scope: { source: "gateway", workspace_ids: [] },
  reference: { gateway_id: "gateway", log_id: "gateway-log", device: "1", inode: "2", line_start: 0, line_end: 30,
    content_version: "a".repeat(64), start_codepoint: start, end_codepoint: start + 2 } });
const response = (body: GatewayDiagnosticLogLocateRequest, text = "开头😀词后续😀词") => Response.json({ request_id: "req_location", data: {
  source: { log_id: "gateway-log", source: "gateway", service: "gateway", label: "原日志", workspace_ids: [], status: "available" },
  reference: body.reference, text,
} });
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
afterEach(restoreGatewayTestEnvironment);

test("只读取目标原行，Unicode 码点高亮且切换位置即时隐藏旧正文", async () => {
  const calls: Array<{ body: GatewayDiagnosticLogLocateRequest; signal?: AbortSignal | null; resolve: (value: Response) => void }> = [];
  installGatewayFetch(({ path, init }) => {
    expect(path).toBe("/api/gateway/diagnostics/locate");
    return new Promise<Response>(resolve => calls.push({ body: JSON.parse(String(init?.body)), signal: init?.signal, resolve }));
  });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticLogLocation apiPort={8011} request={request()} workspaces={[]} onClose={() => {}} />); });
  await flush();
  await act(async () => { calls[0].resolve(response(calls[0].body)); });
  await flush();
  expect(renderer.root.findByType("mark").children).toEqual(["😀词"]);
  const next = request(6);
  act(() => { renderer.update(<GatewayDiagnosticLogLocation apiPort={8011} request={next} workspaces={[]} onClose={() => {}} />); });
  expect(renderer.root.findAllByType("mark")).toHaveLength(0);
  expect(JSON.stringify(renderer.toJSON())).not.toContain("开头😀词");
  await flush();
  await act(async () => { calls[1].resolve(response(calls[1].body)); });
  await flush();
  expect(renderer.root.findByType("mark").children).toEqual(["😀词"]);
  expect(renderer.root.findByType("mark").props["data-start-codepoint"]).toBe(6);
  const pending = request();
  act(() => { renderer.update(<GatewayDiagnosticLogLocation apiPort={8011} request={pending} workspaces={[]} onClose={() => {}} />); });
  await flush();
  act(() => renderer.unmount());
  expect(calls[2].signal?.aborted).toBe(true);
});

test("失效引用明确报错并释放旧原文，不回读日志 tail", async () => {
  installGatewayFetch(({ path }) => {
    expect(path).toBe("/api/gateway/diagnostics/locate");
    return Response.json({ detail: { owner: "runtime_log", code: "stale_runtime_log_reference", reason: "record_changed", message: "原行已改写" } }, { status: 409 });
  });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticLogLocation apiPort={8011} request={request()} workspaces={[]} onClose={() => {}} />); });
  await flush();
  expect(JSON.stringify(renderer.root.findByProps({ role: "alert" }).children)).toContain("原行已改写");
  expect(renderer.root.findAllByType("pre")).toHaveLength(0);
  act(() => renderer.unmount());
});

test("切换时取消旧读取，迟到原文不能覆盖新位置", async () => {
  const calls: Array<{ body: GatewayDiagnosticLogLocateRequest; signal?: AbortSignal | null; resolve: (value: Response) => void }> = [];
  installGatewayFetch(({ path, init }) => {
    expect(path).toBe("/api/gateway/diagnostics/locate");
    return new Promise<Response>(resolve => calls.push({ body: JSON.parse(String(init?.body)), signal: init?.signal, resolve }));
  });
  let renderer!: ReactTestRenderer;
  act(() => { renderer = create(<GatewayDiagnosticLogLocation apiPort={8011} request={request()} workspaces={[]} onClose={() => {}} />); });
  await flush();
  const next = request(6);
  act(() => { renderer.update(<GatewayDiagnosticLogLocation apiPort={8011} request={next} workspaces={[]} onClose={() => {}} />); });
  await flush();
  expect(calls[0].signal?.aborted).toBe(true);
  await act(async () => { calls[1].resolve(response(calls[1].body)); });
  await flush();
  expect(renderer.root.findByType("mark").props["data-start-codepoint"]).toBe(6);
  await act(async () => { calls[0].resolve(response(calls[0].body, "旧原文😀词")); });
  await flush();
  expect(JSON.stringify(renderer.toJSON())).not.toContain("旧原文");
  expect(renderer.root.findByType("mark").props["data-start-codepoint"]).toBe(6);
  act(() => renderer.unmount());
});
