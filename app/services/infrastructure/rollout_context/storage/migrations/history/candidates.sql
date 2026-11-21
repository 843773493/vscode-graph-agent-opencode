WITH page_candidates AS MATERIALIZED (
    SELECT refs.view_id, refs.block_ordinal, membership.logical_item_ordinal,
        item.item_id, item.projection_manifest_json
    FROM context_view_item_blocks AS refs
    CROSS JOIN context_membership_items AS membership ON membership.block_id = refs.block_id
    CROSS JOIN item_catalog AS item ON item.item_id = membership.item_id
    CROSS JOIN executions AS execution ON execution.execution_id = item.execution_id
    WHERE refs.view_id = :view_id
        AND membership.visible = 1 AND membership.source_kind = 'display_only'
        AND item.turn_id IS NULL AND execution.execution_origin = 'internal'
        AND execution.turn_id IS NULL
        AND membership.logical_item_ordinal <= :upper
        AND (:execution_id IS NULL OR execution.execution_id = :execution_id)
        {page_predicates}
    ORDER BY refs.block_ordinal {order}, membership.logical_item_ordinal {order}
    LIMIT :candidate_limit
)
