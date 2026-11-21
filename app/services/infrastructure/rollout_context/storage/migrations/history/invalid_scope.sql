SELECT item_id FROM carriers
WHERE json_array_length(identity, '$.content_part_refs') > 0
    AND (SELECT COUNT(DISTINCT scope) FROM (
        SELECT json_extract(carriers.identity, '$.model_call_id') AS scope
        UNION ALL
        SELECT json_extract(relation.value, '$.target_model_call_id')
        FROM json_each(carriers.projection_manifest_json, '$.provenance_relations') AS relation
        WHERE json_extract(relation.value, '$.relation') = 'replaces'
            AND json_extract(relation.value, '$.target_kind') = 'projection_message'
    )) != 1
LIMIT 1
