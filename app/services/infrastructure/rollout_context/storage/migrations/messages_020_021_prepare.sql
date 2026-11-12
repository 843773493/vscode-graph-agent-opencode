ALTER TABLE messages RENAME TO messages_v20;
DROP INDEX messages_turn_index;
DROP INDEX messages_role_index;
DROP INDEX messages_offset_index;
DROP INDEX messages_execution_index;
