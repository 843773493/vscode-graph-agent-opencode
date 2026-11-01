import assert from "node:assert/strict";

const reasoningRefTypes = new Set(["reasoning", "reasoning_content"]);
const textRefTypes = new Set(["text", "output_text"]);
const contentCarrierKinds = new Set(["assistant_output", "reasoning"]);

function parseMembershipRow(row) {
  assert.equal(typeof row.item_id, "string");
  assert.ok(row.item_id.length > 0);
  assert.ok(Number.isInteger(row.logical_item_ordinal) && row.logical_item_ordinal >= 0);
  assert.equal(row.visible, 1);
  assert.equal(row.source_kind, "display_only");
  assert.equal(typeof row.semantic_kind, "string");

  const manifest = JSON.parse(row.projection_manifest_json);
  assert.ok(manifest && typeof manifest === "object" && !Array.isArray(manifest));
  assert.ok(Array.isArray(manifest.provenance_relations));
  const identity = manifest.projection_identity;
  assert.ok(identity === null || (identity && typeof identity === "object" && !Array.isArray(identity)));
  return { row, manifest, identity };
}

export function expectedTypedInternalHistoryMembership(rows) {
  assert.ok(Array.isArray(rows) && rows.length > 0);
  const itemIds = new Set();
  const ordinals = new Set();
  const records = rows.map(parseMembershipRow);
  for (const { row } of records) {
    assert.ok(!itemIds.has(row.item_id), `重复的 display-only item: ${row.item_id}`);
    assert.ok(!ordinals.has(row.logical_item_ordinal), `重复的 logical item ordinal: ${row.logical_item_ordinal}`);
    itemIds.add(row.item_id);
    ordinals.add(row.logical_item_ordinal);
  }

  const finalCarriers = records.filter(({ row, identity }) =>
    row.semantic_kind === "assistant_output" && Array.isArray(identity?.content_part_refs)
      && identity.content_part_refs.length > 0);
  assert.equal(finalCarriers.length, 1, "必须唯一匹配带 typed content refs 的 final carrier");
  const finalCarrierRecord = finalCarriers[0];
  const { row: finalRow, manifest: finalManifest, identity: finalIdentity } = finalCarrierRecord;
  assert.equal(finalIdentity.phase, "final_answer");
  assert.equal(typeof finalIdentity.model_call_id, "string");
  assert.ok(finalIdentity.model_call_id.length > 0);

  const refs = finalIdentity.content_part_refs;
  assert.equal(refs.length, 2, "最终 carrier 必须只有 reasoning 与 text 两个 typed part");
  assert.deepEqual(refs.map((ref) => ref.index), [0, 1]);
  assert.ok(refs.every((ref) => typeof ref.id === "string" && ref.id.length > 0));
  assert.equal(new Set(refs.map((ref) => ref.id)).size, refs.length);
  assert.ok(reasoningRefTypes.has(refs[0].type), `unexpected reasoning ref type: ${refs[0].type}`);
  assert.ok(textRefTypes.has(refs[1].type), `unexpected text ref type: ${refs[1].type}`);

  const modelCallId = finalIdentity.model_call_id;
  const shadowIds = new Set();
  for (const [partOrdinal, ref] of refs.entries()) {
    const matches = records.filter(({ row, identity }) => contentCarrierKinds.has(row.semantic_kind)
      && identity?.model_call_id === modelCallId
      && identity.block_id === `${modelCallId}:block:${ref.id}`
      && identity.block_index === ref.index);
    assert.equal(matches.length, 1, `typed content ref 必须唯一命中原 carrier: ${ref.id}`);
    assert.equal(matches[0].row.semantic_kind, partOrdinal === 0 ? "reasoning" : "assistant_output");
    shadowIds.add(matches[0].row.item_id);
  }

  const replacements = finalManifest.provenance_relations.filter((relation) => relation.relation === "replaces");
  assert.equal(replacements.length, 1, "final carrier 必须有且只有一个 typed replaces 关系");
  const replacement = replacements[0];
  assert.equal(replacement.target_kind, "projection_message");
  assert.equal(replacement.target_model_call_id, modelCallId);
  assert.equal(typeof replacement.target_id, "string");
  assert.ok(replacement.target_id.length > 0);
  const replacementTargets = records.filter(({ row, identity }) => row.semantic_kind === "assistant_output"
    && identity?.model_call_id === modelCallId
    && identity.projection_message_id === replacement.target_id);
  assert.equal(replacementTargets.length, 1, "replaces 必须唯一命中同一 ModelCall 的 checkpoint carrier");
  assert.notEqual(replacementTargets[0].row.item_id, finalRow.item_id);
  shadowIds.add(replacementTargets[0].row.item_id);
  assert.equal(shadowIds.size, 3, "typed refs 与 replaces 必须命中三个不同的 shadow item");

  const visibleMembership = records
    .filter(({ row }) => !shadowIds.has(row.item_id))
    .map(({ row }) => [row.item_id, row.logical_item_ordinal])
    .sort((left, right) => left[1] - right[1]);
  assert.ok(visibleMembership.some(([itemId]) => itemId === finalRow.item_id), "canonical final carrier 必须保留");
  return {
    finalCarrier: { item_id: finalRow.item_id, model_call_id: modelCallId },
    visibleMembership,
  };
}
