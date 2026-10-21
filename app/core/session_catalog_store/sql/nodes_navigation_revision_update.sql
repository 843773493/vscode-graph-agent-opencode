
    CREATE TRIGGER IF NOT EXISTS nodes_navigation_revision_update
    AFTER UPDATE OF node_id, kind, parent_node_id, display_name, title_source,
                    state, revision,
                    workspace_id, created_at, storage_relative_locator, main_thread_id,
                    session_kind, current_agent_id, current_provider_id,
                    context_source_session_id, generation_origin_json, updated_at
    ON nodes
    WHEN OLD.node_id IS NOT NEW.node_id
      OR OLD.kind IS NOT NEW.kind
      OR OLD.parent_node_id IS NOT NEW.parent_node_id
      OR OLD.display_name IS NOT NEW.display_name
      OR OLD.title_source IS NOT NEW.title_source
      OR OLD.state IS NOT NEW.state
      OR OLD.revision IS NOT NEW.revision
      OR OLD.workspace_id IS NOT NEW.workspace_id
      OR OLD.created_at IS NOT NEW.created_at
      OR OLD.storage_relative_locator IS NOT NEW.storage_relative_locator
      OR OLD.main_thread_id IS NOT NEW.main_thread_id
      OR OLD.session_kind IS NOT NEW.session_kind
      OR OLD.current_agent_id IS NOT NEW.current_agent_id
      OR OLD.current_provider_id IS NOT NEW.current_provider_id
      OR OLD.context_source_session_id IS NOT NEW.context_source_session_id
      OR OLD.generation_origin_json IS NOT NEW.generation_origin_json
      OR OLD.updated_at IS NOT NEW.updated_at
    BEGIN
        UPDATE catalog_metadata
        SET navigation_revision = navigation_revision + 1
        WHERE singleton_id = 1;
        SELECT CASE WHEN changes() != 1
            THEN RAISE(ABORT, 'session catalog navigation revision row missing')
        END;
    END
