, replaced_messages AS MATERIALIZED (
    SELECT DISTINCT json_extract(relation.target_ref, '$.target_id') AS message_id,
        COALESCE(json_extract(relation.target_ref, '$.target_model_call_id'), carrier.part_scope) AS model_call_id
    FROM carriers AS carrier
    CROSS JOIN item_relations AS relation ON relation.canonical_item_id = carrier.item_id
    WHERE relation.relation = 'replaces'
        AND json_extract(relation.target_ref, '$.target_kind') = 'projection_message'
        AND (json_extract(carrier.projection_manifest_json, '$.projection_group') IS NULL
            OR json_extract(carrier.projection_manifest_json, '$.projection_group.ordinal') =
                json_extract(carrier.projection_manifest_json, '$.projection_group.size') - 1)
), replaced_content_groups AS MATERIALIZED (
    SELECT DISTINCT target.message_group_id
    FROM replaced_messages replacement
    JOIN item_catalog target ON
        json_extract(target.projection_manifest_json, '$.projection_identity.projection_message_id') = replacement.message_id
        AND json_extract(target.projection_manifest_json, '$.projection_identity.model_call_id') IS replacement.model_call_id
    WHERE target.message_group_id IS NOT NULL
), replaced_parts AS MATERIALIZED (
    SELECT DISTINCT carrier.part_scope,
        json_extract(part.value, '$.index') AS block_index,
        carrier.part_scope || ':block:' || json_extract(part.value, '$.id') AS block_id
    FROM carriers AS carrier, json_each(carrier.identity, '$.content_part_refs') AS part
    WHERE carrier.part_scope IS NOT NULL
)
