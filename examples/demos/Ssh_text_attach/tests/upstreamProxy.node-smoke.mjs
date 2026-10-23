import assert from "node:assert/strict";
import { createServer, request as httpRequest } from "node:http";
import { HttpError } from "../src/server/http.js";
import { proxyBackendRequest } from "../src/server/upstreamProxy.js";

const socketsByServer = new WeakMap();

function listen(server) {
  const sockets = new Set();
  socketsByServer.set(server, sockets);
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      const address = server.address();
      assert(address && typeof address !== "string");
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

function wait(promise, timeoutMs, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (error) => { clearTimeout(timer); reject(error); },
    );
  });
}

async function close(server) {
  if (!server.listening) return;
  const sockets = socketsByServer.get(server);
  if (!sockets) {
    throw new Error("HTTP smoke 服务未通过 listen fixture 初始化");
  }
  const closed = new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });
  for (const socket of sockets) {
    socket.destroy();
  }
  await closed;
}

function writeRepeatedRequestBody(request, totalBytes, chunkSizeBytes = 64 * 1024) {
  return new Promise((resolve, reject) => {
    let remainingBytes = totalBytes;
    let responseReceived = false;
    let drainHandler = null;
    let settled = false;

    const finish = (error) => {
      if (settled) {
        return;
      }
      settled = true;
      if (drainHandler) {
        request.off("drain", drainHandler);
        drainHandler = null;
      }
      request.off("response", onResponse);
      request.off("error", onError);
      request.off("close", onClose);
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    };
    const onResponse = () => {
      responseReceived = true;
      finish();
    };
    const onError = (error) => finish(responseReceived ? null : error);
    const onClose = () => {
      if (responseReceived || request.writableEnded) {
        finish();
        return;
      }
      finish(new Error("超限请求在收到 Gateway 响应前关闭"));
    };
    const writeNextChunk = () => {
      drainHandler = null;
      while (remainingBytes > 0 && !responseReceived && !request.destroyed) {
        const chunkLength = Math.min(remainingBytes, chunkSizeBytes);
        remainingBytes -= chunkLength;
        if (!request.write(Buffer.alloc(chunkLength, 1))) {
          drainHandler = writeNextChunk;
          request.once("drain", drainHandler);
          return;
        }
      }

      if (responseReceived) {
        finish();
      } else if (request.destroyed) {
        finish(new Error("超限请求在发送完整请求体前关闭"));
      } else {
        request.end();
        finish();
      }
    };

    request.once("response", onResponse);
    request.once("error", onError);
    request.once("close", onClose);
    writeNextChunk();
  });
}

let upstreamStartedResolve;
let upstreamClosedResolve;
let uploadFirstChunkResolve;
let oversizedUploadAbortedResolve;
const upstreamStarted = new Promise((resolve) => { upstreamStartedResolve = resolve; });
const upstreamClosed = new Promise((resolve) => { upstreamClosedResolve = resolve; });
const uploadFirstChunk = new Promise((resolve) => { uploadFirstChunkResolve = resolve; });
const oversizedUploadAborted = new Promise((resolve) => { oversizedUploadAbortedResolve = resolve; });
const upstreamServer = createServer((request, response) => {
  const pathname = new URL(request.url || "/", "http://upstream.test").pathname;
  if (pathname === "/headers") {
    const requestBodyChunks = [];
    request.on("data", (chunk) => requestBodyChunks.push(chunk));
    request.once("end", () => {
      response.writeHead(200, {
        "content-type": "application/json",
        connection: "x-upstream-private",
        "proxy-connection": "x-upstream-proxy-private",
        "x-upstream-private": "drop-connection-option",
        "x-upstream-proxy-private": "drop-proxy-connection-option",
        "x-upstream-end-to-end": "preserve",
      });
      response.end(JSON.stringify({
        requestConnectionOption: request.headers["x-request-private"] ?? null,
        requestProxyConnectionOption: request.headers["x-proxy-private"] ?? null,
        requestEndToEndHeader: request.headers["x-request-end-to-end"] ?? null,
        requestContentLength: request.headers["content-length"] ?? null,
        requestTransferEncoding: request.headers["transfer-encoding"] ?? null,
        requestBody: Buffer.concat(requestBodyChunks).toString("utf8"),
      }));
    });
    return;
  }
  if (pathname === "/oversized") {
    request.once("aborted", oversizedUploadAbortedResolve);
    request.resume();
    return;
  }
  response.writeHead(200, { "content-type": "text/plain" });
  if (pathname === "/upload") {
    let receivedBytes = 0;
    request.on("data", (chunk) => {
      receivedBytes += chunk.length;
      uploadFirstChunkResolve();
    });
    request.on("end", () => {
      response.end(`${receivedBytes}:${request.headers["content-length"] || "none"}`);
    });
    return;
  }
  if (pathname === "/complete") {
    response.end("complete response");
    return;
  }
  response.once("close", upstreamClosedResolve);
  response.write("pending response");
  upstreamStartedResolve();
});
const upstreamOrigin = await listen(upstreamServer);
const proxyErrors = [];
const expectedRequestErrors = [];
let firstProxyFinishedResolve;
let secondProxyFinishedResolve;
let thirdProxyFinishedResolve;
let fourthProxyFinishedResolve;
let fifthProxyFinishedResolve;
const firstProxyFinished = new Promise((resolve) => { firstProxyFinishedResolve = resolve; });
const secondProxyFinished = new Promise((resolve) => { secondProxyFinishedResolve = resolve; });
const thirdProxyFinished = new Promise((resolve) => { thirdProxyFinishedResolve = resolve; });
const fourthProxyFinished = new Promise((resolve) => { fourthProxyFinishedResolve = resolve; });
const fifthProxyFinished = new Promise((resolve) => { fifthProxyFinishedResolve = resolve; });
let proxyOperationCount = 0;
const gatewayServer = createServer((request, response) => {
  const originalUrl = new URL(request.url || "/", `http://${request.headers.host}`);
  void proxyBackendRequest(
    { backendOriginFor: async () => upstreamOrigin },
    { targetId: "fixture", backendPath: originalUrl.pathname },
    request,
    response,
    originalUrl,
  ).catch((error) => {
    if (error instanceof HttpError && error.statusCode === 413) {
      expectedRequestErrors.push(error);
    } else {
      proxyErrors.push(error);
    }
    if (response.headersSent) response.destroy();
    else response.writeHead(error.statusCode ?? 500).end(error.message);
  }).finally(() => {
    proxyOperationCount += 1;
    if (proxyOperationCount === 1) firstProxyFinishedResolve();
    if (proxyOperationCount === 2) secondProxyFinishedResolve();
    if (proxyOperationCount === 3) thirdProxyFinishedResolve();
    if (proxyOperationCount === 4) fourthProxyFinishedResolve();
    if (proxyOperationCount === 5) fifthProxyFinishedResolve();
  });
});
const gatewayOrigin = await listen(gatewayServer);
const clientAbort = new AbortController();
let uploadRequest;
let uploadResponsePromise;

try {
  const clientResponse = await wait(fetch(`${gatewayOrigin}/stream`, {
    signal: clientAbort.signal,
  }), 1500, "downstream response headers did not arrive");
  await wait(upstreamStarted, 1500, "upstream request did not start");
  const bodyRead = clientResponse.text();
  clientAbort.abort();
  const bodyError = await wait(
    bodyRead.catch((error) => error),
    1500,
    "downstream abort was not observed",
  );
  await wait(upstreamClosed, 1500, "upstream response did not close");
  await wait(firstProxyFinished, 1500, "cancelled proxy operation did not finish");
  assert.equal(bodyError.name, "AbortError");

  const uploadBodyBytes = 64 * 1024 + 3;
  uploadResponsePromise = new Promise((resolve, reject) => {
    uploadRequest = httpRequest(new URL("/upload", gatewayOrigin), {
      method: "POST",
      headers: { "content-length": String(uploadBodyBytes) },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        statusCode: response.statusCode,
        body: Buffer.concat(chunks).toString("utf8"),
      }));
    });
    uploadRequest.once("error", reject);
    uploadRequest.write(Buffer.alloc(64 * 1024, 1));
  });
  await wait(uploadFirstChunk, 1500, "上游未在请求体结束前收到首段数据");
  uploadRequest.end(Buffer.from([2, 3, 4]));
  const uploadResponse = await wait(uploadResponsePromise, 1500, "流式请求未完成");
  assert.deepEqual(uploadResponse, {
    statusCode: 200,
    body: `${uploadBodyBytes}:${uploadBodyBytes}`,
  });
  await wait(secondProxyFinished, 1500, "流式代理操作未完成");

  const oversizedRequestBodyBytes = 20 * 1024 * 1024 + 1;
  let oversizedRequest;
  const oversizedResponsePromise = new Promise((resolve, reject) => {
    oversizedRequest = httpRequest(new URL("/oversized", gatewayOrigin), {
      method: "POST",
      headers: { "content-length": String(oversizedRequestBodyBytes) },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        statusCode: response.statusCode,
        body: Buffer.concat(chunks).toString("utf8"),
      }));
    });
    oversizedRequest.once("error", reject);
  });
  const [oversizedResponse] = await Promise.all([
    wait(oversizedResponsePromise, 3000, "超限请求没有被拒绝"),
    wait(
      writeRepeatedRequestBody(oversizedRequest, oversizedRequestBodyBytes),
      3000,
      "超限请求体写入未收敛",
    ),
  ]);
  assert.equal(oversizedResponse.statusCode, 413);
  assert.match(oversizedResponse.body, /20971520 bytes/);
  await wait(oversizedUploadAborted, 1500, "超限请求未取消远端的部分上传");
  await wait(thirdProxyFinished, 1500, "超限代理操作未完成");

  const completeResponse = await fetch(`${gatewayOrigin}/complete`);
  assert.equal(completeResponse.status, 200);
  assert.equal(await completeResponse.text(), "complete response");
  await wait(fourthProxyFinished, 1500, "completed proxy operation did not finish");

  const headerForwardingResponse = await new Promise((resolve, reject) => {
    const headerForwardingRequest = httpRequest(new URL("/headers", gatewayOrigin), {
      method: "POST",
      headers: {
        connection: "x-request-private, content-length",
        "proxy-connection": "x-proxy-private",
        "x-request-private": "drop-connection-option",
        "x-proxy-private": "drop-proxy-connection-option",
        "x-request-end-to-end": "preserve",
        "content-length": "3",
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        statusCode: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
        headers: response.headers,
      }));
    });
    headerForwardingRequest.once("error", reject);
    headerForwardingRequest.end("abc");
  });
  assert.equal(headerForwardingResponse.statusCode, 200);
  assert.deepEqual(headerForwardingResponse.body, {
    requestConnectionOption: null,
    requestProxyConnectionOption: null,
    requestEndToEndHeader: "preserve",
    requestContentLength: null,
    requestTransferEncoding: "chunked",
    requestBody: "abc",
  });
  assert.equal(headerForwardingResponse.headers["x-upstream-private"], undefined);
  assert.equal(headerForwardingResponse.headers["x-upstream-proxy-private"], undefined);
  assert.equal(headerForwardingResponse.headers["x-upstream-end-to-end"], "preserve");
  await wait(fifthProxyFinished, 1500, "Connection 点名头代理操作未完成");

  assert.deepEqual(proxyErrors, []);
  assert.equal(expectedRequestErrors.length, 1);
  console.log("downstream_abort_observed=true");
  console.log("upstream_response_closed=true");
  console.log("proxy_finished_without_error=true");
  console.log("upload_streamed_before_body_complete=true");
  console.log("oversized_body_rejected=true");
  console.log("normal_response_completed=true");
  console.log("connection_options_stripped=true");
} finally {
  uploadRequest?.destroy();
  await uploadResponsePromise?.catch(() => undefined);
  clientAbort.abort();
  await Promise.all([close(gatewayServer), close(upstreamServer)]);
}
