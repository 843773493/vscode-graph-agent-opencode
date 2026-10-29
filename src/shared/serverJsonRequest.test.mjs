import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { test } from "node:test";
import { createJsonRequestReader } from "./serverJsonRequest.mjs";

function requestFor(body) {
  // 包含逐字节 UTF-8 分块，避免把 HTTP chunk 当成完整文本解码。
  return Readable.from([...Buffer.from(body)].map((byte) => Buffer.from([byte])));
}

for (const [body, expected] of [
  ["", {}], [" \r\n\t", {}], ["null", null], ["false", false], ["42", 42],
  ['"用户 /parse-secret"', "用户 /parse-secret"], ['[1,"中文"]', [1, "中文"]],
  ['{"url":"file:///parse-secret.html","title":"中文"}', { url: "file:///parse-secret.html", title: "中文" }],
]) {
  test(`正常 JSON 及空正文保持原值: ${body}`, async () => {
    const readJson = createJsonRequestReader({ error() { assert.fail("正常请求不写错误日志"); } });
    assert.deepEqual(await readJson(requestFor(body)), expected);
  });
}

for (const body of ["/parse-secret", "C:\\parse-secret", "\\\\host\\parse-secret"]) {
  test(`解析失败保留同一原始 cause: ${body}`, async () => {
    const logs = [];
    const readJson = createJsonRequestReader({ error(...values) { logs.push(values); } });
    await assert.rejects(readJson(requestFor(body)), (error) => {
      assert(error instanceof SyntaxError);
      assert.equal(error.message, "HTTP JSON 请求解析失败: stage=request_json, error=SyntaxError");
      assert.equal(logs.length, 1);
      assert.equal(error.cause, logs[0].at(-1));
      assert(error.cause instanceof SyntaxError);
      // Bun 与 Node 的原生说明不同；两者都必须保留当前引擎的完整原说明。
      assert.throws(() => JSON.parse(body), (native) => native.message === error.cause.message);
      assert.deepEqual(logs[0][1], { stage: "request_json" });
      return true;
    });
  });
}

test("读取流失败保留原异常，不伪装成 JSON 解析失败", async () => {
  const original = new Error("原始传输中断");
  const readJson = createJsonRequestReader({ error() { assert.fail("读取失败不进入解析出口"); } });
  const request = Readable.from((async function* () {
    yield Buffer.from("{");
    throw original;
  })());
  await assert.rejects(readJson(request), (error) => error === original);
});

test("诊断失败仍拒绝请求并保留解析与日志两份原异常", async () => {
  const sinkError = new Error("/log-secret");
  let original;
  const readJson = createJsonRequestReader({ error(_message, _facts, cause) {
    original = cause;
    throw sinkError;
  } });
  await assert.rejects(readJson(requestFor("/parse-secret")), (error) => {
    assert(error instanceof SyntaxError);
    assert.equal(error.message, "HTTP JSON 请求解析失败: stage=request_json, error=SyntaxError");
    assert(error.cause instanceof AggregateError);
    assert.deepEqual(error.cause.errors, [original, sinkError]);
    assert.equal(error.cause.cause, original);
    return true;
  });
});
