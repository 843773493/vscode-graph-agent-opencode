WITH carriers AS NOT MATERIALIZED (
    SELECT membership.view_id, membership.logical_item_ordinal,
        item.item_id, item.projection_manifest_json,
        json_extract(item.projection_manifest_json, '$.projection_identity') AS identity,
        COALESCE(
            json_extract(item.projection_manifest_json, '$.projection_identity.model_call_id'),
            (SELECT json_extract(relation.value, '$.target_model_call_id')
             FROM json_each(item.projection_manifest_json, '$.provenance_relations') AS relation
             WHERE json_extract(relation.value, '$.relation') = 'replaces'
                AND json_extract(relation.value, '$.target_kind') = 'projection_message'
                AND json_extract(relation.value, '$.target_model_call_id') IS NOT NULL
             LIMIT 1)
        ) AS part_scope
    FROM context_view_items AS membership
    JOIN item_catalog AS item ON item.item_id = membership.item_id
    JOIN executions AS execution ON execution.execution_id = item.execution_id
    WHERE membership.view_id = :view_id
        AND membership.visible = 1 AND membership.source_kind = 'display_only'
        AND item.turn_id IS NULL AND execution.execution_origin = 'internal'
        AND execution.turn_id IS NULL
        AND membership.logical_item_ordinal <= :snapshot_upper
        AND (:execution_id IS NULL OR execution.execution_id = :execution_id)
)
