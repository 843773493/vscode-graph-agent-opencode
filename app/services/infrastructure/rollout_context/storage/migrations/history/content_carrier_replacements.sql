, replaced_messages AS MATERIALIZED (
    SELECT DISTINCT json_extract(relation.target_ref, '$.target_id') AS message_id
    FROM carriers AS carrier
    CROSS JOIN item_relations AS relation ON relation.canonical_item_id = carrier.item_id
    WHERE relation.relation = 'replaces'
        AND json_extract(relation.target_ref, '$.target_kind') = 'projection_message'
), replaced_parts AS MATERIALIZED (
    SELECT DISTINCT carrier.part_scope,
        json_extract(part.value, '$.index') AS block_index,
        carrier.part_scope || ':block:' || json_extract(part.value, '$.id') AS block_id
    FROM carriers AS carrier, json_each(carrier.identity, '$.content_part_refs') AS part
    WHERE carrier.part_scope IS NOT NULL
)
