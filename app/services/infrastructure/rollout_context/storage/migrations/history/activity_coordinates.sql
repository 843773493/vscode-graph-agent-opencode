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
), indexed_parts AS MATERIALIZED (
    SELECT item.item_id, item.logical_item_ordinal,
        json_extract(part.value,'$.part_ordinal') AS part_ordinal,
        json_extract(part.value,'$.kind') AS kind, segmented.segment
    FROM surviving_items item
    JOIN segmented ON segmented.item_id=item.item_id AND segmented.barrier=0
    LEFT JOIN item_catalog anchor ON anchor.item_id=COALESCE((
        SELECT tail.item_id FROM item_catalog tail WHERE tail.message_group_id=item.message_group_id
            AND json_extract(tail.projection_manifest_json,'$.projection_group.ordinal')=
                json_extract(tail.projection_manifest_json,'$.projection_group.size')-1),item.item_id)
    LEFT JOIN messages message ON message.jsonl_offset=anchor.jsonl_offset AND message.jsonl_length=anchor.jsonl_length
    LEFT JOIN message_projections projection ON projection.message_sequence=message.message_sequence
    JOIN json_each(COALESCE((SELECT value FROM json_each(projection.response_outline_json) WHERE key=item.item_id),
        json_array(json_object('part_ordinal', COALESCE(json_extract(item.projection_manifest_json,'$.projection_identity.block_index'),0),
            'kind', CASE WHEN item.semantic_kind='assistant_output' THEN 'text'
                WHEN item.semantic_kind='reasoning' AND item.payload_kind='summary' THEN 'reasoning_summary'
                WHEN item.semantic_kind='reasoning' AND item.payload_kind IN ('opaque','extension') THEN 'reasoning_encrypted'
                ELSE item.semantic_kind END)))) part
    WHERE item.semantic_kind!='runtime_notice'
), boundaries AS (
    SELECT *, SUM(kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'))
        OVER (PARTITION BY segment ORDER BY logical_item_ordinal,part_ordinal) AS activity_group
    FROM indexed_parts
), coordinates AS (
    SELECT *, FIRST_VALUE(item_id) OVER (PARTITION BY segment ORDER BY logical_item_ordinal,part_ordinal) AS segment_item,
        FIRST_VALUE(item_id) OVER (PARTITION BY segment,activity_group ORDER BY
            kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal,part_ordinal) AS group_item,
        FIRST_VALUE(part_ordinal) OVER (PARTITION BY segment,activity_group ORDER BY
            kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal,part_ordinal) AS group_part
    FROM boundaries
)
SELECT item_id,part_ordinal,segment_item,group_item,group_part,kind FROM coordinates
WHERE item_id IN (SELECT value FROM json_each(:coordinate_items))
