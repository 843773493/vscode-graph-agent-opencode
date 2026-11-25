, surviving_items AS MATERIALIZED (
    SELECT item.*, membership.logical_item_ordinal
    FROM carriers AS membership
    JOIN item_catalog AS item ON item.item_id=membership.item_id
    WHERE {survivor}
), segment_events AS MATERIALIZED (
    SELECT item_id,logical_item_ordinal,0 AS barrier FROM surviving_items
    UNION ALL
    SELECT item.item_id,membership.logical_item_ordinal,1
    FROM context_view_item_blocks refs
    CROSS JOIN context_membership_items membership ON membership.block_id=refs.block_id
    CROSS JOIN item_catalog item ON item.item_id=membership.item_id
    WHERE refs.view_id=:view_id AND membership.visible=1
        AND membership.logical_item_ordinal BETWEEN
            (SELECT MIN(logical_item_ordinal) FROM surviving_items) AND
            (SELECT MAX(logical_item_ordinal) FROM surviving_items)
        AND (item.turn_scope='turn_root' OR (membership.source_kind='display_only'
            AND (item.execution_id IS NOT :execution_id OR item.semantic_kind='runtime_notice')))
), segmented AS (
    SELECT item_id,barrier,SUM(barrier) OVER (ORDER BY logical_item_ordinal,barrier) AS segment
    FROM segment_events
), indexed_items AS MATERIALIZED (
    SELECT item.item_id,item.logical_item_ordinal,
        CASE WHEN item.semantic_kind='assistant_output' THEN 'text'
            WHEN item.semantic_kind='reasoning' THEN reasoning.kind
            ELSE item.semantic_kind END AS kind,segmented.segment
    FROM surviving_items item
    JOIN segmented ON segmented.item_id=item.item_id AND segmented.barrier=0
    LEFT JOIN reasoning_blocks reasoning ON reasoning.item_id=item.item_id
    WHERE item.semantic_kind!='runtime_notice'
), boundaries AS (
    SELECT *,SUM(kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'))
        OVER (PARTITION BY segment ORDER BY logical_item_ordinal) AS activity_group
    FROM indexed_items
), coordinates AS (
    SELECT *,FIRST_VALUE(item_id) OVER (PARTITION BY segment ORDER BY logical_item_ordinal) AS segment_item,
        FIRST_VALUE(item_id) OVER (PARTITION BY segment,activity_group ORDER BY
            kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal) AS group_item
    FROM boundaries
)
SELECT item_id,segment_item,group_item,kind FROM coordinates
WHERE item_id IN (SELECT value FROM json_each(:coordinate_items))
