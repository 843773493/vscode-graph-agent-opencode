
CREATE TABLE IF NOT EXISTS lifecycle_fence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    generation INTEGER NOT NULL
)
