WITH replaced_items AS MATERIALIZED (
    SELECT json_extract(relation.target_ref, '$.target_id') AS item_id
    FROM item_relations AS relation
    WHERE relation.canonical_item_id IS NOT NULL AND relation.relation = 'replaces'
        AND json_extract(relation.target_ref, '$.target_kind') = 'canonical_item'
        AND EXISTS (
            SELECT 1 FROM context_view_items AS carrier
            WHERE carrier.item_id = relation.canonical_item_id AND carrier.view_id = :view_id
                AND carrier.visible = 1 AND carrier.source_kind = 'canonical'
        )
), replaced_groups AS MATERIALIZED (
    SELECT item.message_group_id FROM item_catalog AS item
    JOIN replaced_items AS replacement ON replacement.item_id = item.item_id
    WHERE item.message_group_id IS NOT NULL
), carriers AS NOT MATERIALIZED (
    SELECT refs.view_id, refs.block_ordinal, membership.logical_item_ordinal,
        item.item_id, item.message_group_id, item.projection_manifest_json,
        json_extract(item.projection_manifest_json, '$.projection_identity') AS identity,
        COALESCE(
            json_extract(item.projection_manifest_json, '$.projection_identity.model_call_id'),
            (SELECT json_extract(target_ref, '$.target_model_call_id') FROM item_relations
             WHERE canonical_item_id = item.item_id AND relation = 'replaces'
                AND json_extract(target_ref, '$.target_kind') = 'projection_message'
                AND json_extract(target_ref, '$.target_model_call_id') IS NOT NULL
             ORDER BY canonical_relation_ordinal LIMIT 1)
        ) AS part_scope
    FROM context_view_item_blocks AS refs
    CROSS JOIN context_membership_items AS membership ON membership.block_id = refs.block_id
    CROSS JOIN item_catalog AS item ON item.item_id = membership.item_id
    WHERE refs.view_id = :view_id AND membership.visible = 1 AND membership.source_kind = 'canonical'
        AND item.item_id NOT IN (SELECT item_id FROM replaced_items)
        AND (item.message_group_id IS NULL OR item.message_group_id NOT IN (SELECT message_group_id FROM replaced_groups))
)
