WITH members AS MATERIALIZED (
    SELECT item.*, membership.logical_item_ordinal,
        json_extract(item.projection_manifest_json,'$.projection_identity') AS identity,
        record.final_item_id,
        CASE WHEN selected.final_message_sequence IS NOT NULL
            AND json_extract(final.projection_manifest_json,'$.projection_group') IS NOT NULL
            THEN final.message_group_id END AS final_message_group_id
    FROM item_catalog item
    CROSS JOIN context_view_items membership ON membership.item_id=item.item_id
        AND membership.view_id=:view_id AND membership.visible=1 AND membership.source_kind='canonical'
    JOIN turn_records record ON record.turn_id=item.turn_id
    JOIN context_view_turns selected ON selected.turn_id=item.turn_id AND selected.view_id=:view_id
    LEFT JOIN item_catalog final ON final.item_id=record.final_item_id
    WHERE item.turn_id IN (SELECT value FROM json_each(:turn_ids))
        AND item.semantic_kind IN ('reasoning','tool_call','tool_result','compaction_summary','assistant_output')
), carriers AS MATERIALIZED (
    SELECT *,json_extract(identity,'$.model_call_id') AS part_scope FROM members
)
{replacements}
, surviving_items AS MATERIALIZED (
    SELECT item.*,
        CASE WHEN item.semantic_kind='assistant_output' THEN 'text'
            WHEN item.semantic_kind='reasoning' THEN reasoning.kind
            ELSE item.semantic_kind END AS kind,
        (item.semantic_kind='assistant_output' AND (
            item.item_id IS item.final_item_id OR (
                item.final_message_group_id IS NOT NULL AND item.message_group_id IS item.final_message_group_id
            )
        )) AS is_final
    FROM carriers item
    LEFT JOIN reasoning_blocks reasoning ON reasoning.item_id=item.item_id
    WHERE {survivor}
), ranked AS (
    SELECT *,ROW_NUMBER() OVER (
        PARTITION BY turn_id,semantic_kind,part_scope,
            CASE WHEN semantic_kind IN ('tool_call','tool_result') AND part_scope IS NOT NULL THEN
                CASE WHEN substr(tool_call_id,1,length(part_scope)+11)=part_scope||':tool-call:'
                    THEN substr(tool_call_id,length(part_scope)+12) ELSE tool_call_id END
                ELSE item_id END
        ORDER BY logical_item_ordinal
    ) AS representative
    FROM surviving_items
), boundaries AS (
    SELECT *,SUM(NOT is_final AND kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'))
        OVER (PARTITION BY turn_id ORDER BY logical_item_ordinal) AS activity_group,
        MAX(CASE WHEN NOT is_final AND kind!='reasoning_encrypted' THEN logical_item_ordinal END)
            OVER (PARTITION BY turn_id ORDER BY logical_item_ordinal
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS previous_counted_ordinal
    FROM ranked WHERE representative=1
), coordinates AS (
    SELECT *,FIRST_VALUE(item_id) OVER (PARTITION BY turn_id,activity_group ORDER BY
        kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal) AS group_item
    FROM boundaries
), stats AS (
    SELECT turn_id,COUNT(*) FILTER (WHERE NOT is_final AND kind!='reasoning_encrypted') AS item_count,
        MIN(item_sequence) FILTER (WHERE NOT is_final AND kind!='reasoning_encrypted') AS first_item_sequence,
        MAX(item_sequence) FILTER (WHERE NOT is_final AND kind!='reasoning_encrypted') AS last_item_sequence
    FROM coordinates GROUP BY turn_id
), page AS (
    SELECT item.item_id,item.kind,item.logical_item_ordinal,previous.created_at AS previous_created_at,
        item.group_item,item.turn_id,NULL AS item_count,NULL AS first_item_sequence,NULL AS last_item_sequence,item.is_final
    FROM coordinates item
    LEFT JOIN coordinates previous ON previous.turn_id=item.turn_id
        AND previous.logical_item_ordinal=item.previous_counted_ordinal
    WHERE (:item_ids IS NULL OR item.item_id IN (SELECT value FROM json_each(:item_ids)))
        AND (:before_item IS NULL OR item.logical_item_ordinal<:before_item)
        AND (:include_final OR NOT item.is_final)
    ORDER BY item.logical_item_ordinal DESC LIMIT :item_limit
)
SELECT * FROM page
UNION ALL
SELECT NULL,NULL,NULL,NULL,NULL,turn_id,item_count,first_item_sequence,last_item_sequence,0 FROM stats
