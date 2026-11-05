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

export function expectedForkPrefixParts(canonical, members, turn) {
  // 先按冻结 selection 裁原件，再解析 carrier；源 view 已去重的展示部件不能代替所选原件。
  const ordinals = new Map(members.map(member => [member.item_id, member.logical_item_ordinal]));
  const items = canonical.map(entry => entry.item).filter(item =>
    item.turn_id === turn.turn_id && ordinals.has(item.item_id));
  const candidates = new Map();
  const callOrdinals = new Map();
  const add = (item, kind, text, ordinal, key, preferred = false, detail = {}) => {
    assert.equal(typeof text, "string");
    const part = { part_id: `${item.item_id}:part:${ordinal}`, kind, text, ...detail,
      source: { item_id: item.item_id, item_sequence: item.item_sequence,
        logical_item_ordinal: ordinals.get(item.item_id), part_ordinal: ordinal,
        created_at: item.created_at.replace(/\+00:00$/, "Z"), elapsed_ms: 0 }, preferred };
    const previous = candidates.get(key);
    if (previous) assert.deepEqual([part.kind, part.text, part.arguments, part.result],
      [previous.kind, previous.text, previous.arguments, previous.result], "同 scope carrier 的正文与工具参数必须一致");
    if (!candidates.has(key) || preferred) candidates.set(key, part);
  };
  for (const item of items) {
    const identity = item.projection_identity ?? {};
    const scope = identity.model_call_id ?? item.item_id;
    const blockIndex = identity.block_index ?? item.projection_group?.ordinal ?? 0;
    const canonicalCall = value => value?.replace(`${scope}:tool-call:`, "");
    if (item.semantic_kind === "reasoning") {
      assert.equal(item.payload_kind, "text", "该真实 fork 场景的独立 oracle 只接受明确的文本 reasoning");
      add(item, "reasoning", item.payload, 0, `reasoning:${scope}:${blockIndex}`, !!identity.block_id);
    } else if (item.semantic_kind === "tool_call") {
      const calls = item.payload.tool_calls ?? [item.payload];
      calls.forEach((call, index) => {
        const callId = call.tool_call_id ?? call.id;
        assert.equal(typeof callId, "string");
        const ordinal = identity.block_id ? identity.block_index : index;
        callOrdinals.set(`${scope}:${canonicalCall(callId)}`, ordinal);
        add(item, "tool_call", call.name, ordinal, `tool_call:${scope}:${canonicalCall(callId)}`,
          !!identity.block_id, { tool_call_id: callId, arguments: call.args });
      });
    } else if (item.semantic_kind === "tool_result") {
      const callId = item.tool_result_evidence?.tool_call_id ?? item.payload.tool_call_id;
      assert.equal(typeof callId, "string");
      const ordinal = callOrdinals.get(`${scope}:${canonicalCall(callId)}`);
      assert.ok(Number.isInteger(ordinal), "selected 工具结果必须有同 scope 的先行调用");
      add(item, "tool_result", item.payload.content, ordinal, `tool_result:${scope}:${canonicalCall(callId)}`,
        !identity.projection_message_id, { tool_call_id: callId, result: item.payload.content });
    } else if (item.semantic_kind === "compaction_summary") {
      add(item, "compaction_summary", item.payload.summary, 0, item.item_id);
    } else if (item.item_id === turn.final_item_id) {
      const blocks = typeof item.payload === "string" ? [{ type: "text", text: item.payload }] : item.payload;
      assert.ok(Array.isArray(blocks));
      blocks.forEach((block, index) => {
        if (block.type === "reasoning_content") add(item, "reasoning", block.reasoning_content,
          index * 1_000_000, `reasoning:${scope}:${index}`);
        else assert.ok(["text", "output_text"].includes(block.type), `未覆盖的 final block: ${block.type}`);
      });
      const text = blocks.filter(block => ["text", "output_text"].includes(block.type)).map(block => block.text).join("");
      add(item, "final_text", text, 1_000_000_000, `${item.item_id}:final`);
      candidates.get(`${item.item_id}:final`).part_id = `${item.item_id}:final`;
    } else assert.ok(["user_input", "assistant_output", "runtime_notice"].includes(item.semantic_kind),
      `未覆盖的 prefix canonical kind: ${item.semantic_kind}`);
  }
  const parts = [...candidates.values()].sort((left, right) =>
    left.source.logical_item_ordinal - right.source.logical_item_ordinal || left.source.part_ordinal - right.source.part_ordinal);
  let previous = timestampMicros(turn.projection_start);
  for (const part of parts) {
    const created = timestampMicros(part.source.created_at);
    if (part.kind !== "final_text") {
      assert.ok(created >= previous, "source canonical activity 时间不得倒退");
      part.source.elapsed_ms = Number((created - previous) / 1000n);
      previous = created;
    }
    delete part.preferred;
  }
  return parts;
}
