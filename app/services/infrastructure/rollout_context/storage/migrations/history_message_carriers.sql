WITH visible_items AS MATERIALIZED (
    SELECT membership.view_id, membership.logical_item_ordinal,
        item.item_id, item.message_group_id, item.projection_manifest_json,
        json_extract(item.projection_manifest_json, '$.projection_identity') AS identity
    FROM context_view_items AS membership
    JOIN item_catalog AS item ON item.item_id = membership.item_id
    WHERE membership.view_id = :view_id
        AND membership.visible = 1 AND membership.source_kind = 'canonical'
), replaced_items AS MATERIALIZED (
    SELECT json_extract(relation.value, '$.target_id') AS item_id
    FROM visible_items AS carrier,
        json_each(carrier.projection_manifest_json, '$.provenance_relations') AS relation
    WHERE json_extract(relation.value, '$.relation') = 'replaces'
        AND json_extract(relation.value, '$.target_kind') = 'canonical_item'
), replaced_groups AS MATERIALIZED (
    SELECT item.message_group_id FROM item_catalog AS item
    JOIN replaced_items AS replacement ON replacement.item_id = item.item_id
    WHERE item.message_group_id IS NOT NULL
), carriers AS MATERIALIZED (
    SELECT visible_items.*,
        COALESCE(
            json_extract(identity, '$.model_call_id'),
            (SELECT json_extract(relation.value, '$.target_model_call_id')
             FROM json_each(projection_manifest_json, '$.provenance_relations') AS relation
             WHERE json_extract(relation.value, '$.relation') = 'replaces'
                AND json_extract(relation.value, '$.target_kind') = 'projection_message'
                AND json_extract(relation.value, '$.target_model_call_id') IS NOT NULL
             LIMIT 1)
        ) AS part_scope
    FROM visible_items
    WHERE item_id NOT IN (SELECT item_id FROM replaced_items)
        AND (message_group_id IS NULL OR message_group_id NOT IN (SELECT message_group_id FROM replaced_groups))
)
