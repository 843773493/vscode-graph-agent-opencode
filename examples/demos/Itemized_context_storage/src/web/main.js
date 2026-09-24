const state = await fetch("/api/state").then(async (response) => {
  if (!response.ok) throw new Error(`读取 demo 状态失败: HTTP ${response.status}`);
  return response.json();
});

const byId = (id) => document.getElementById(id);
const pretty = (value) => JSON.stringify(value, null, 2);
const escapeHtml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

byId("runtime-root").textContent = state.runtimeRoot;
byId("jsonl").textContent = state.rolloutJsonl;
byId("plan").textContent = pretty(state.plan);
byId("transcript").textContent = pretty(state.transcript);

const includedCount = state.view.filter((item) => item.included).length;
const metrics = [
  ["canonical items", state.catalog.length, "JSONL 中的 item 数量"],
  ["active view", `${includedCount}/${state.view.length}`, "included / traced"],
  ["wire messages", state.plan.wire_request.messages.length, "发送给 Provider 的消息投影"],
  ["transcript entries", state.transcript.entries.length, "用户可见的粗粒度历史"],
];
byId("metrics").innerHTML = metrics.map(([label, value, detail]) => `
  <div class="metric"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span><small>${escapeHtml(detail)}</small></div>
`).join("");

byId("catalog").querySelector("tbody").innerHTML = state.catalog.map((item) => `
  <tr><td>${item.item_sequence}</td><td><code>${escapeHtml(item.item_id)}</code></td><td>${escapeHtml(item.semantic_kind)}</td><td>${item.jsonl_offset}</td><td>${item.jsonl_length}</td></tr>
`).join("");

byId("view").querySelector("tbody").innerHTML = state.view.map((item) => `
  <tr><td>${item.item_sequence}</td><td>${escapeHtml(item.semantic_kind)}</td><td><span class="${item.included ? "included" : "omitted"}">${item.included ? "included" : "omitted"}</span></td><td>${escapeHtml(item.omission_reason ?? "加入 wire request")}</td></tr>
`).join("");

byId("files").innerHTML = state.files.map((file) => `
  <li><code>${escapeHtml(file.path)}</code><span>${file.bytes} B</span></li>
`).join("");
