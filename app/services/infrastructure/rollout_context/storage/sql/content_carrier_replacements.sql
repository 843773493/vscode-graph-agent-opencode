, replaced_messages AS MATERIALIZED (
    SELECT DISTINCT json_extract(relation.value, '$.target_id') AS message_id
    FROM carriers AS carrier,
        json_each(carrier.projection_manifest_json, '$.provenance_relations') AS relation
    WHERE json_extract(relation.value, '$.relation') = 'replaces'
        AND json_extract(relation.value, '$.target_kind') = 'projection_message'
), replaced_parts AS MATERIALIZED (
    SELECT DISTINCT carrier.part_scope,
        json_extract(part.value, '$.index') AS block_index,
        carrier.part_scope || ':block:' || json_extract(part.value, '$.id') AS block_id
    FROM carriers AS carrier, json_each(carrier.identity, '$.content_part_refs') AS part
    WHERE carrier.part_scope IS NOT NULL
)
