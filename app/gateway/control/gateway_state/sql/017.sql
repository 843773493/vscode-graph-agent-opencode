
    CREATE TABLE native_directory_roots (
        gateway_id TEXT NOT NULL,
        directory_id TEXT NOT NULL,
        native_root TEXT NOT NULL,
        PRIMARY KEY (gateway_id, directory_id),
        UNIQUE (gateway_id, native_root)
    );
