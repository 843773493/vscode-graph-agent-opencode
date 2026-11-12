CREATE TABLE IF NOT EXISTS context_membership_blocks (
    block_id INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS context_membership_items (
    block_id INTEGER NOT NULL REFERENCES context_membership_blocks(block_id),
    member_index INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    logical_item_ordinal INTEGER NOT NULL,
    visible INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    context_selected INTEGER NOT NULL CHECK(context_selected IN (0,1)),
    context_ordinal INTEGER,
    PRIMARY KEY(block_id,member_index)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS membership_items_identity ON context_membership_items(item_id,block_id);
CREATE INDEX IF NOT EXISTS membership_items_ordinal ON context_membership_items(block_id,logical_item_ordinal);
CREATE TABLE IF NOT EXISTS context_view_item_blocks (
    view_id TEXT NOT NULL,
    block_ordinal INTEGER NOT NULL,
    block_id INTEGER NOT NULL REFERENCES context_membership_blocks(block_id),
    PRIMARY KEY(view_id,block_ordinal)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS view_item_blocks_source ON context_view_item_blocks(block_id,view_id);
CREATE VIEW IF NOT EXISTS context_view_items AS
SELECT refs.view_id, member.item_id, member.logical_item_ordinal, member.visible,
    member.source_kind, member.context_selected, member.context_ordinal
FROM context_view_item_blocks refs JOIN context_membership_items member ON member.block_id=refs.block_id;
