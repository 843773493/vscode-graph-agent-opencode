import { createConnection } from "node:net";
import { readFile } from "node:fs/promises";

export async function testControl(payload) {
  const address = JSON.parse(process.env.BOXTEAM_E2E_TEST_CONTROL);
  return new Promise((resolve, reject) => {
    const connection = createConnection(address.port, address.host);
    connection.setEncoding("utf8");
    let buffer = "";
    connection.on("connect", () => connection.write(JSON.stringify({ ...payload, token: address.token, pid: process.pid }) + "\n"));
    connection.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      connection.end();
      const result = JSON.parse(buffer.slice(0, newline));
      if (result.error) reject(new Error(result.error));
      else resolve(result.value);
    });
    connection.on("error", reject);
    connection.on("end", () => { if (!buffer.includes("\n")) reject(new Error("外置 test control 未返回完整回执")); });
  });
}

export async function planGoldenLoop(sessionId, threadId) {
  const cassette = JSON.parse(await readFile("tests/fixtures/model_stream/handwritten/openai_chat/basic-chat-tool-loop.json", "utf8"));
  // 完整产品的资源与通知各占独立消息；回放按精确调用身份分配原始 SSE。
  const interactions = cassette.interactions.map((interaction) => ({
    ...interaction,
    request: { ...interaction.request, match: {
      model: interaction.request.match.model,
      stream: interaction.request.match.stream,
    } },
  }));
  return testControl({ action: "plan_model", session_id: sessionId, thread_id: threadId, interactions });
}

/** 原始 Provider SSE fixture；产品 parser、工具与存储仍由真实应用执行。 */
export function chatInteraction({ label, reasoning = "", text = "", calls = [], usage }) {
  const frames = [];
  const push = (delta, finishReason = null) => frames.push({
    kind: "data", encoding: "json", payload: {
      id: label, object: "chat.completion.chunk", created: 1, model: "big-pickle",
      choices: [{ index: 0, delta, finish_reason: finishReason }],
    },
  });
  push({ role: "assistant", ...(reasoning ? { reasoning_content: reasoning } : {}) });
  for (const [index, call] of calls.entries()) {
    push({ tool_calls: [{ index, id: call.id, type: "function", function: { name: call.name, arguments: JSON.stringify(call.arguments ?? {}) } }] });
  }
  if (text) push({ content: text });
  push({}, calls.length ? "tool_calls" : "stop");
  if (usage) frames.push({ kind: "data", encoding: "json", payload: {
    id: label, object: "chat.completion.chunk", created: 1, model: "big-pickle", choices: [],
    usage: { prompt_tokens: usage.input, completion_tokens: usage.output, total_tokens: usage.input + usage.output },
  } });
  frames.push({ kind: "done", encoding: "text", payload: "[DONE]" });
  return {
    request: { method: "POST", url: "https://opencode.ai/zen/v1/chat/completions", match: { model: "big-pickle", stream: true } },
    response: { status: 200, headers: { "content-type": "text/event-stream" }, frames },
  };
}

export async function planChatCalls(sessionId, threadId, responses) {
  return testControl({ action: "plan_model", session_id: sessionId, thread_id: threadId, interactions: responses.map(chatInteraction) });
}

export function uuidText(hex) {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
