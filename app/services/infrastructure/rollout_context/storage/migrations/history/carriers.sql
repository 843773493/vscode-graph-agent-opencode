, needed_scopes AS MATERIALIZED (
    SELECT DISTINCT json_extract(projection_manifest_json, '$.projection_identity.model_call_id') AS scope
    FROM page_candidates
    WHERE json_extract(projection_manifest_json, '$.projection_identity.model_call_id') IS NOT NULL
), replacement_items AS MATERIALIZED (
    SELECT item_id FROM item_catalog
    WHERE json_extract(projection_manifest_json, '$.projection_identity.model_call_id') IN (SELECT scope FROM needed_scopes)
        AND json_array_length(projection_manifest_json, '$.projection_identity.content_part_refs') > 0
    UNION
    SELECT canonical_item_id FROM item_relations
    WHERE canonical_item_id IS NOT NULL AND relation = 'replaces'
        AND json_extract(target_ref, '$.target_kind') = 'projection_message'
        AND (json_extract(target_ref, '$.target_model_call_id') IN (SELECT scope FROM needed_scopes)
            OR json_extract(target_ref, '$.target_id') IN (
                SELECT json_extract(projection_manifest_json, '$.projection_identity.projection_message_id')
                FROM page_candidates
                WHERE json_extract(projection_manifest_json, '$.projection_identity.projection_message_id') IS NOT NULL
            ))
), carriers AS MATERIALIZED (
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
    FROM (SELECT item_id FROM page_candidates UNION SELECT item_id FROM replacement_items) AS selected
    CROSS JOIN item_catalog AS item ON item.item_id = selected.item_id
    CROSS JOIN executions AS execution ON execution.execution_id = item.execution_id
    CROSS JOIN context_view_items AS membership ON membership.item_id = item.item_id
    WHERE membership.view_id = :view_id AND membership.visible = 1
        AND membership.source_kind = 'display_only'
        AND item.turn_id IS NULL AND execution.execution_origin = 'internal' AND execution.turn_id IS NULL
        AND +membership.logical_item_ordinal <= :snapshot_upper
        AND (:execution_id IS NULL OR execution.execution_id = :execution_id)
)
