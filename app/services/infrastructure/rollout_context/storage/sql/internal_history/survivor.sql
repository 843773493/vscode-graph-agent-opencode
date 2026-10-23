NOT (item.semantic_kind IN ('assistant_output', 'reasoning')
    AND json_extract(item.projection_manifest_json, '$.projection_identity') IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM carriers AS replacement
        WHERE EXISTS (
            SELECT 1 FROM json_each(replacement.projection_manifest_json, '$.provenance_relations') AS relation
            WHERE json_extract(relation.value, '$.relation') = 'replaces'
                AND json_extract(relation.value, '$.target_kind') = 'projection_message'
                AND json_extract(relation.value, '$.target_id') =
                    json_extract(item.projection_manifest_json, '$.projection_identity.projection_message_id')
        ) OR (
            replacement.part_scope = json_extract(item.projection_manifest_json, '$.projection_identity.model_call_id')
            AND EXISTS (
                SELECT 1 FROM json_each(replacement.identity, '$.content_part_refs') AS part
                WHERE json_extract(part.value, '$.index') =
                        json_extract(item.projection_manifest_json, '$.projection_identity.block_index')
                    AND replacement.part_scope || ':block:' || json_extract(part.value, '$.id') =
                        json_extract(item.projection_manifest_json, '$.projection_identity.block_id')
            )
        )
    )
)
