import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { HttpError, streamLimitedRequestBody } from "./http.js";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "expect",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "upgrade",
]);
const HTTP_BODY_FRAMING_HEADERS = new Set(["content-length", "transfer-encoding"]);

function connectionHeaderOptions(...headerValues) {
  const options = new Set();
  for (const headerValue of headerValues) {
    const values = Array.isArray(headerValue) ? headerValue : [headerValue];
    for (const value of values) {
      if (typeof value !== "string") {
        continue;
      }
      for (const option of value.split(",")) {
        const normalizedOption = option.trim().toLowerCase();
        if (normalizedOption) {
          options.add(normalizedOption);
        }
      }
    }
  }
  return options;
}

function requestHopByHopHeaders(request) {
  return new Set([
    ...HOP_BY_HOP_HEADERS,
    ...connectionHeaderOptions(
      request.headers.connection,
      request.headers["proxy-connection"],
    ),
  ]);
}

function proxyRequestHeaders(request, hopByHopHeaders) {
  const headers = {};
  for (const [name, value] of Object.entries(request.headers)) {
    const normalizedName = name.toLowerCase();
    if (
      hopByHopHeaders.has(normalizedName)
      || HTTP_BODY_FRAMING_HEADERS.has(normalizedName)
      || value === undefined
    ) {
      continue;
    }
    headers[name] = Array.isArray(value) ? value.join(", ") : value;
  }
  return headers;
}

function proxyResponseHeaders(upstreamResponse) {
  const headers = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "access-control-allow-headers": "content-type",
    "cache-control": "no-store",
  };
  const hopByHopHeaders = new Set([
    ...HOP_BY_HOP_HEADERS,
    ...connectionHeaderOptions(
      upstreamResponse.headers.get("connection"),
      upstreamResponse.headers.get("proxy-connection"),
    ),
  ]);
  for (const [name, value] of upstreamResponse.headers) {
    const normalizedName = name.toLowerCase();
    if (
      !hopByHopHeaders.has(normalizedName)
      && !HTTP_BODY_FRAMING_HEADERS.has(normalizedName)
    ) {
      headers[name] = value;
    }
  }
  return headers;
}

export async function proxyBackendRequest(targetClient, backendProxyRoute, request, response, originalUrl) {
  if (response.destroyed) {
    return;
  }

  const abortController = new AbortController();
  const abortUpstreamOnDisconnect = () => {
    if (!response.writableFinished) {
      abortController.abort();
    }
  };
  response.once("close", abortUpstreamOnDisconnect);

  try {
    const origin = await targetClient.backendOriginFor(backendProxyRoute.targetId);
    const upstreamUrl = new URL(`${backendProxyRoute.backendPath}${originalUrl.search}`, origin);
    const hasBody = request.method !== "GET" && request.method !== "HEAD";
    const body = hasBody ? streamLimitedRequestBody(request) : undefined;
    const hopByHopHeaders = requestHopByHopHeaders(request);
    const headers = proxyRequestHeaders(request, hopByHopHeaders);
    const contentLength = request.headers["content-length"];
    if (hasBody && contentLength !== undefined && !hopByHopHeaders.has("content-length")) {
      headers["content-length"] = contentLength;
    }
    const upstreamResponse = await fetch(upstreamUrl, {
      method: request.method,
      headers,
      body,
      ...(hasBody ? { duplex: "half" } : {}),
      signal: abortController.signal,
    });
    response.writeHead(upstreamResponse.status, proxyResponseHeaders(upstreamResponse));
    if (upstreamResponse.body) {
      await pipeline(Readable.fromWeb(upstreamResponse.body), response);
      return;
    }
    response.end();
  } catch (error) {
    if (response.destroyed) {
      abortController.abort();
      return;
    }
    throw error instanceof Error && error.cause instanceof HttpError
      ? error.cause
      : error;
  } finally {
    response.off("close", abortUpstreamOnDisconnect);
  }
}
