CREATE TEMP TABLE fork_selected_turns AS SELECT DISTINCT turn_id FROM item_catalog WHERE item_id IN (SELECT item_id FROM fork_selected_items) AND turn_id IS NOT NULL
