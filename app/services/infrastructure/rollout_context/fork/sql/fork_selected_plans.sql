CREATE TEMP TABLE fork_selected_plans AS SELECT plan_id FROM context_assemblies WHERE assembly_id IN (SELECT assembly_id FROM fork_selected_assemblies)
