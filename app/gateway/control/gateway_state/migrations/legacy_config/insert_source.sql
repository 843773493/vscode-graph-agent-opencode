
                INSERT INTO config_source_layers(
                    config_key, uri, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json
                ) VALUES (?, NULL, 'present', ?, ?, 1, ?, 1, NULL, ?, NULL)
