INSERT INTO checkpoint_state_values (
    session_id, thread_id, checkpoint_ns, channel_name, channel_version,
    serializer_name, value_blob, value_length, value_hash
)
SELECT DISTINCT source.session_id, source.thread_id, source.checkpoint_ns,
    channel.channel_name, channel.channel_version, channel.serializer_name,
    channel.value_blob, channel.value_length, channel.value_hash
FROM checkpoint_channels_v19 channel
JOIN checkpoint_context_source_snapshots source ON source.checkpoint_id=channel.checkpoint_id
WHERE channel.value_state='present';

INSERT INTO checkpoint_channels (
    checkpoint_id, channel_name, storage_kind, value_state, channel_version,
    value_id, value_session_id, value_thread_id, context_view_id, updated_index, created_at
)
SELECT channel.checkpoint_id, channel.channel_name, channel.storage_kind,
    channel.value_state, channel.channel_version, value.value_id, value.session_id, value.thread_id,
    channel.context_view_id, channel.updated_index, channel.created_at
FROM checkpoint_channels_v19 channel
LEFT JOIN checkpoint_context_source_snapshots source ON source.checkpoint_id=channel.checkpoint_id
LEFT JOIN checkpoint_state_values value ON value.session_id=source.session_id
    AND value.thread_id=source.thread_id AND value.checkpoint_ns=source.checkpoint_ns
    AND value.channel_name=channel.channel_name AND value.channel_version=channel.channel_version
    AND channel.value_state='present';

DROP TABLE checkpoint_channels_v19;
