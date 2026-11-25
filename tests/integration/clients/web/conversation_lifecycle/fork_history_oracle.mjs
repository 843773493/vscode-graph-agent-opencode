import assert from "node:assert/strict";
import { digestJcsJson } from "../../../../fixtures/itemized/verify_hash_vectors.mjs";

export function freezeForkCanonicalItems(rows, members) {
  assert.equal(rows.length, members.length);
  const byId = new Map(members.map(member => [member.item_id, member]));
  return rows.map(({ bytes }) => {
    const item = JSON.parse(bytes);
    assert.equal(item.format_version, 2);
    assert.equal(item.record_type, "item");
    const member = byId.get(item.item_id);
    assert.ok(member, "canonical 原件必须属于冻结 view");
    assert.deepEqual([item.item_sequence, item.turn_id ?? null, item.created_at],
      [member.item_sequence, member.turn_id, member.created_at]);
    assert.equal(item.content_hash, digestJcsJson({ payload_kind: item.payload_kind, payload: item.payload }));
    byId.delete(item.item_id);
    return { bytes, item };
  });
}

function timestampMicros(value) {
  const match = /^(.*?)(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  assert.ok(match, `canonical 时间必须有时区: ${value}`);
  return BigInt(Date.parse(match[1] + match[3])) * 1000n + BigInt((match[2] ?? "").padEnd(6, "0"));
}

export function expectedForkPrefixItems(canonical, members, turn) {
  // 以请求前冻结的原件独立推导；不从目标 history 反推期望，也不生成 Part 身份。
  const ordinals = new Map(members.map(member => [member.item_id, member.logical_item_ordinal]));
  const items = canonical.map(entry => entry.item).filter(item =>
    item.turn_id === turn.turn_id && ordinals.has(item.item_id));
  const final = items.find(item => item.item_id === turn.final_item_id);
  const finalIds = new Set(items.filter(item => item.semantic_kind === "assistant_output"
    && final && (item.item_id === final.item_id || final.message_group_id
      && item.message_group_id === final.message_group_id)).map(item => item.item_id));
  const candidates = new Map();
  const seenCalls = new Set();
  const add = (item, kind, text, key, preferred = false, detail = {}) => {
    assert.equal(typeof text, "string");
    const value = { id: item.item_id, kind, text, ...detail,
      source: { item_id: item.item_id, item_sequence: item.item_sequence,
        logical_item_ordinal: ordinals.get(item.item_id),
        created_at: item.created_at.replace(/\+00:00$/, "Z"), elapsed_ms: 0 } };
    const previous = candidates.get(key);
    if (previous) assert.deepEqual([value.kind, value.text, value.arguments, value.result],
      [previous.kind, previous.text, previous.arguments, previous.result], "同 scope carrier 的正文与工具参数必须一致");
    if (!previous || preferred) candidates.set(key, value);
  };
  for (const item of items) {
    const identity = item.projection_identity ?? {};
    const scope = identity.model_call_id ?? item.item_id;
    const blockIndex = identity.block_index ?? item.projection_group?.ordinal ?? 0;
    const canonicalCall = value => value?.replace(`${scope}:tool-call:`, "");
    if (item.semantic_kind === "reasoning") {
      assert.equal(item.payload_kind, "text", "该 fork fixture 的独立 oracle 只接受明确的文本 reasoning");
      add(item, "reasoning", item.payload, `reasoning:${scope}:${blockIndex}`, !!identity.block_id);
    } else if (item.semantic_kind === "tool_call") {
      const calls = item.payload.tool_calls ?? [item.payload];
      assert.equal(calls.length, 1, "每个 canonical Item 只声明一次工具调用");
      const [call] = calls;
      const callId = call.tool_call_id ?? call.id;
      assert.equal(typeof callId, "string");
      seenCalls.add(`${scope}:${canonicalCall(callId)}`);
      add(item, "tool_call", call.name, `tool_call:${scope}:${canonicalCall(callId)}`,
        !!identity.block_id, { tool_call_id: callId, arguments: call.args });
    } else if (item.semantic_kind === "tool_result") {
      const callId = item.tool_result_evidence?.tool_call_id ?? item.payload.tool_call_id;
      assert.equal(typeof callId, "string");
      assert.ok(seenCalls.has(`${scope}:${canonicalCall(callId)}`), "selected 工具结果必须有同 scope 的先行调用");
      add(item, "tool_result", item.payload.content, `tool_result:${scope}:${canonicalCall(callId)}`,
        !identity.projection_message_id, { tool_call_id: callId, result: item.payload.content });
    } else if (item.semantic_kind === "compaction_summary") {
      add(item, "compaction_summary", item.payload.summary, item.item_id);
    } else if (item.semantic_kind === "assistant_output") {
      const blocks = typeof item.payload === "string" ? [{ type: "text", text: item.payload }] : item.payload;
      assert.ok(Array.isArray(blocks) && blocks.length === 1, "fixture 的输出 Item 必须是独立文本块");
      assert.ok(["text", "output_text"].includes(blocks[0].type));
      add(item, finalIds.has(item.item_id) ? "final_text" : "text", blocks[0].text, item.item_id);
    } else assert.ok(["user_input", "runtime_notice"].includes(item.semantic_kind),
      `未覆盖的 prefix canonical kind: ${item.semantic_kind}`);
  }
  const result = [...candidates.values()].sort((left, right) =>
    left.source.logical_item_ordinal - right.source.logical_item_ordinal);
  let previous = timestampMicros(turn.projection_start);
  for (const value of result) {
    const created = timestampMicros(value.source.created_at);
    if (value.kind !== "final_text") {
      assert.ok(created >= previous, "source canonical activity 时间不得倒退");
      value.source.elapsed_ms = Number((created - previous) / 1000n);
      previous = created;
    }
  }
  return result;
}
