WITH requested AS MATERIALIZED (
    SELECT item.item_id,item.semantic_kind,item.execution_id,item.tool_call_id,
        json_extract(item.projection_manifest_json,'$.projection_identity.model_call_id') AS model_call_id
    FROM item_catalog item
    WHERE item.item_id IN (SELECT value FROM json_each(:item_ids))
        AND item.semantic_kind IN ('tool_call','tool_result')
), calls AS (
    SELECT *,CASE WHEN substr(tool_call_id,1,length(model_call_id)+11)=model_call_id||':tool-call:'
        THEN substr(tool_call_id,length(model_call_id)+12) ELSE tool_call_id END AS raw_id
    FROM requested
)
SELECT DISTINCT declaration.turn_id,declaration.item_id,tool.tool_call_id,tool.tool_name,tool.status,
    CASE WHEN EXISTS (
        SELECT 1 FROM messages result
        JOIN item_catalog result_item ON result_item.jsonl_offset=result.jsonl_offset
            AND result_item.jsonl_length=result.jsonl_length
        JOIN context_view_items result_member ON result_member.item_id=result_item.item_id
            AND result_member.view_id=:view_id AND result_member.visible=1
            AND result_member.source_kind='canonical'
        WHERE result.message_sequence=tool.result_message_sequence
    ) THEN tool.result_message_sequence END AS result_message_sequence,
    tool.assistant_message_sequence,tool.call_index,declaration.projection_manifest_json
FROM calls
CROSS JOIN item_catalog declaration INDEXED BY item_catalog_model_tool_index
    ON json_extract(declaration.projection_manifest_json,'$.projection_identity.model_call_id') IS calls.model_call_id
    AND declaration.semantic_kind='tool_call' AND declaration.execution_id=calls.execution_id
    AND declaration.tool_call_id IN (calls.raw_id,calls.model_call_id||':tool-call:'||calls.raw_id)
    AND (calls.model_call_id IS NOT NULL OR declaration.item_id=calls.item_id)
JOIN context_view_items declaration_member ON declaration_member.item_id=declaration.item_id
    AND declaration_member.view_id=:view_id AND declaration_member.visible=1
    AND declaration_member.source_kind='canonical'
JOIN item_catalog anchor ON anchor.item_id=COALESCE((
    SELECT tail.item_id FROM item_catalog tail WHERE tail.message_group_id=declaration.message_group_id
        AND json_extract(tail.projection_manifest_json,'$.projection_group.ordinal')=
            json_extract(tail.projection_manifest_json,'$.projection_group.size')-1
),declaration.item_id)
JOIN messages message ON message.jsonl_offset=anchor.jsonl_offset AND message.jsonl_length=anchor.jsonl_length
JOIN tool_calls tool ON tool.assistant_message_sequence=message.message_sequence
    AND tool.tool_call_id IN (calls.raw_id,calls.model_call_id||':tool-call:'||calls.raw_id)
ORDER BY tool.assistant_message_sequence,tool.call_index
