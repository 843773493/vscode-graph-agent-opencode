CREATE TEMP TABLE fork_selected_assemblies(assembly_id TEXT PRIMARY KEY,visited INTEGER NOT NULL DEFAULT 0,expand_body INTEGER NOT NULL DEFAULT -1)
