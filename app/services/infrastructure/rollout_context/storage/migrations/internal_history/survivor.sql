NOT (item.semantic_kind IN ('assistant_output', 'reasoning')
    AND json_extract(item.projection_manifest_json, '$.projection_identity') IS NOT NULL
    AND (EXISTS (
        SELECT 1 FROM replaced_messages AS replacement
        WHERE replacement.message_id =
                json_extract(item.projection_manifest_json, '$.projection_identity.projection_message_id')
    ) OR EXISTS (
        SELECT 1 FROM replaced_parts AS replacement
        WHERE replacement.part_scope = json_extract(item.projection_manifest_json, '$.projection_identity.model_call_id')
            AND replacement.block_index = json_extract(item.projection_manifest_json, '$.projection_identity.block_index')
            AND replacement.block_id = json_extract(item.projection_manifest_json, '$.projection_identity.block_id')
    ))
)
