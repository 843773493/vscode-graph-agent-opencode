WITH members AS MATERIALIZED (
    SELECT item.*, membership.logical_item_ordinal,
        json_extract(item.projection_manifest_json,'$.projection_identity') AS identity,
        record.final_item_id
    FROM item_catalog item
    CROSS JOIN context_view_items membership ON membership.item_id=item.item_id
        AND membership.view_id=:view_id AND membership.visible=1 AND membership.source_kind='canonical'
    JOIN turn_records record ON record.turn_id=item.turn_id
    WHERE item.turn_id IN (SELECT value FROM json_each(:turn_ids))
        AND item.semantic_kind IN ('reasoning','tool_call','tool_result','compaction_summary','assistant_output')
), parts AS MATERIALIZED (
    SELECT item.item_id,item.item_sequence,item.turn_id,item.created_at,item.logical_item_ordinal,
        item.semantic_kind,item.final_item_id,item.identity,
        CASE WHEN item.semantic_kind='reasoning' THEN CASE
            WHEN item.payload_kind='summary' THEN 'reasoning_summary'
            WHEN item.payload_kind IN ('opaque','extension') THEN 'reasoning_encrypted' ELSE 'reasoning' END
            ELSE json_extract(part.value,'$.kind') END AS kind,
        json_extract(part.value,'$.part_ordinal') AS part_ordinal,
        json_extract(part.value,'$.content_block_index') AS content_block_index,
        json_extract(part.value,'$.item_index') AS item_index,
        json_extract(part.value,'$.call_index') AS call_index,
        COALESCE(json_extract(part.value,'$.tool_call_id'),item.tool_call_id) AS tool_call_id,
        json_extract(item.identity,'$.model_call_id') AS model_call_id,
        COALESCE(json_extract(item.identity,'$.block_index'),
            json_extract(part.value,'$.content_block_index'),
            json_extract(item.projection_manifest_json,'$.projection_group.ordinal'),0) AS block_index,
        COALESCE(json_extract(item.identity,'$.block_id'),(
            SELECT json_extract(ref.value,'$.id') FROM json_each(item.identity,'$.content_part_refs') ref
            WHERE json_extract(ref.value,'$.index')=json_extract(part.value,'$.content_block_index')
        )) AS block_id
    FROM members item
    LEFT JOIN item_catalog anchor ON anchor.item_id=COALESCE((
        SELECT tail.item_id FROM item_catalog tail WHERE tail.message_group_id=item.message_group_id
            AND json_extract(tail.projection_manifest_json,'$.projection_group.ordinal')=
                json_extract(tail.projection_manifest_json,'$.projection_group.size')-1),item.item_id)
    LEFT JOIN messages message ON message.jsonl_offset=anchor.jsonl_offset AND message.jsonl_length=anchor.jsonl_length
    LEFT JOIN message_projections projection ON projection.message_sequence=message.message_sequence
    JOIN json_each(COALESCE((SELECT value FROM json_each(projection.response_outline_json) WHERE key=item.item_id),
        json_array(json_object('part_ordinal',0,
            'kind',CASE WHEN item.semantic_kind='assistant_output' THEN 'text'
                WHEN item.semantic_kind='reasoning' AND item.payload_kind='summary' THEN 'reasoning_summary'
                WHEN item.semantic_kind='reasoning' AND item.payload_kind IN ('opaque','extension') THEN 'reasoning_encrypted'
                ELSE item.semantic_kind END)))) part
    WHERE item.item_id IS NOT item.final_item_id OR json_extract(part.value,'$.kind')!='text'
), identities AS (
    SELECT *,CASE
        WHEN model_call_id IS NULL OR kind IN ('text','compaction_summary') THEN json_array(item_id,part_ordinal)
        WHEN kind IN ('tool_call','tool_result') THEN
            COALESCE(CASE WHEN substr(tool_call_id,1,length(model_call_id)+11)=model_call_id||':tool-call:'
                THEN substr(tool_call_id,length(model_call_id)+12) ELSE tool_call_id END,json_array(item_id,part_ordinal))
        ELSE json_array(COALESCE(CASE WHEN substr(block_id,1,length(model_call_id)+7)=model_call_id||':block:'
                THEN substr(block_id,length(model_call_id)+8) ELSE block_id END,CAST(block_index AS TEXT)),COALESCE(item_index,0)) END AS logical_identity
    FROM parts
), ranked AS (
    SELECT *,ROW_NUMBER() OVER (PARTITION BY turn_id,kind,model_call_id,logical_identity ORDER BY item_sequence,part_ordinal) AS representative
    FROM identities part
    WHERE NOT (part.semantic_kind='reasoning' AND part.block_id IS NULL
        AND json_extract(part.identity,'$.projection_message_id') IS NOT NULL
        AND EXISTS (SELECT 1 FROM identities typed WHERE typed.turn_id=part.turn_id AND typed.kind=part.kind
            AND typed.model_call_id=part.model_call_id AND typed.block_index=part.block_index AND typed.block_id IS NOT NULL))
), survivors AS MATERIALIZED (
    SELECT * FROM ranked WHERE representative=1
), boundaries AS (
    SELECT *,SUM(kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'))
        OVER (PARTITION BY turn_id ORDER BY logical_item_ordinal,part_ordinal) AS activity_group,
        LAG(created_at) OVER (PARTITION BY turn_id ORDER BY logical_item_ordinal,part_ordinal) AS previous_created_at
    FROM survivors
), coordinates AS (
    SELECT *,FIRST_VALUE(item_id) OVER (PARTITION BY turn_id,activity_group ORDER BY
        kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal,part_ordinal) AS group_item,
        FIRST_VALUE(part_ordinal) OVER (PARTITION BY turn_id,activity_group ORDER BY
        kind NOT IN ('reasoning','reasoning_summary','reasoning_encrypted','tool_call','tool_result'),logical_item_ordinal,part_ordinal) AS group_part
    FROM boundaries
)
SELECT item_id,part_ordinal,kind,content_block_index,item_index,call_index,logical_item_ordinal,
    previous_created_at,group_item,group_part,turn_id,tool_call_id
FROM coordinates
WHERE (:item_ids IS NULL OR item_id IN (SELECT value FROM json_each(:item_ids)))
    AND (:before_item IS NULL OR logical_item_ordinal<:before_item OR logical_item_ordinal=:before_item AND part_ordinal<:before_part)
ORDER BY logical_item_ordinal DESC,part_ordinal DESC LIMIT :part_limit
