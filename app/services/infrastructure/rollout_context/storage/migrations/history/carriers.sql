WITH carriers AS MATERIALIZED (
    SELECT membership.view_id, membership.logical_item_ordinal,
        item.item_id, item.projection_manifest_json,
        LAG(item.created_at) OVER (
            PARTITION BY item.execution_id ORDER BY membership.logical_item_ordinal
        ) AS previous_created_at,
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
    -- 先按内部执行和 item 索引定位，再核当前 view 的成员；
    -- 不能从整个 view 起扫，否则普通 Turn 历史也会拖慢空的 internal 分页。
    FROM executions AS execution
    CROSS JOIN item_catalog AS item ON item.execution_id = execution.execution_id
    CROSS JOIN context_view_items AS membership ON membership.item_id = item.item_id
    WHERE membership.view_id = :view_id
        AND membership.visible = 1 AND membership.source_kind = 'display_only'
        AND item.turn_id IS NULL AND execution.execution_origin = 'internal'
        AND execution.turn_id IS NULL
        -- 一元 + 禁用 ordinal 范围索引，让已定位 item 走成员身份索引；
        -- 否则 SQLite 展开 view 后会为每个 item 重扫当前 view 的全部块。
        AND +membership.logical_item_ordinal <= :snapshot_upper
        AND (:execution_id IS NULL OR execution.execution_id = :execution_id)
)
