import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createSessionGenerator,
  deleteSessionGenerator,
  listSessionGeneratorRuns,
  listSessionGenerators,
  previewSessionGeneratorPlacement,
  runSessionGenerator,
  updateSessionGenerator,
} from "../../gatewayApi";
import type {
  GenerationRun,
  GeneratorDefinitionCreateRequest,
  GeneratorDefinitionUpdateRequest,
  GeneratorPlacementPreview,
  GeneratorPlacementPreviewRequest,
  SessionGeneratorDefinition,
  SessionGeneratorList,
} from "../../types/backend";

export function useSessionGeneratorResources(apiPort: number) {
  const [generators, setGenerators] = useState<SessionGeneratorList | null>(null);
  const [generationRuns, setGenerationRuns] = useState<Map<string, GenerationRun[]>>(
    new Map(),
  );
  const [generatorError, setGeneratorError] = useState<string | null>(null);

  const refreshGenerators = useCallback(async () => {
    try {
      const next = await listSessionGenerators(apiPort);
      setGenerators(next);
      setGeneratorError(null);
      return next;
    } catch (error) {
      setGeneratorError(error instanceof Error ? error.message : String(error));
      throw error;
    }
  }, [apiPort]);

  const createGenerator = useCallback(async (
    payload: GeneratorDefinitionCreateRequest,
  ): Promise<SessionGeneratorDefinition> => {
    const created = await createSessionGenerator(apiPort, payload);
    await refreshGenerators();
    return created;
  }, [apiPort, refreshGenerators]);

  const refreshGenerationRuns = useCallback(async (generatorId: string) => {
    const result = await listSessionGeneratorRuns(apiPort, generatorId);
    setGenerationRuns((previous) => {
      const next = new Map(previous);
      next.set(generatorId, result.items);
      return next;
    });
    return result.items;
  }, [apiPort]);

  const runGenerator = useCallback(async (generatorId: string): Promise<GenerationRun> => {
    const run = await runSessionGenerator(apiPort, generatorId);
    setGenerationRuns((previous) => {
      const next = new Map(previous);
      const existing = next.get(generatorId) ?? [];
      next.set(
        generatorId,
        [run, ...existing.filter((item) => item.run_id !== run.run_id)],
      );
      return next;
    });
    await refreshGenerators();
    return run;
  }, [apiPort, refreshGenerators]);

  const updateGenerator = useCallback(async (
    generatorId: string,
    payload: GeneratorDefinitionUpdateRequest,
  ): Promise<SessionGeneratorDefinition> => {
    const updated = await updateSessionGenerator(apiPort, generatorId, payload);
    await refreshGenerators();
    return updated;
  }, [apiPort, refreshGenerators]);

  const deleteGenerator = useCallback(async (generatorId: string) => {
    await deleteSessionGenerator(apiPort, generatorId);
    setGenerationRuns((previous) => {
      const next = new Map(previous);
      next.delete(generatorId);
      return next;
    });
    await refreshGenerators();
  }, [apiPort, refreshGenerators]);

  const previewGenerator = useCallback(async (
    payload: GeneratorPlacementPreviewRequest,
  ): Promise<GeneratorPlacementPreview> => {
    return previewSessionGeneratorPlacement(apiPort, payload);
  }, [apiPort]);

  useEffect(() => {
    void refreshGenerators().catch(() => undefined);
  }, [refreshGenerators]);

  useEffect(() => {
    const activeGeneratorIds = [...generationRuns.entries()]
      .filter(([_generatorId, runs]) => runs.some(
        (run) => ["planned", "dispatching", "running", "reporting"].includes(
          run.status,
        ),
      ))
      .map(([generatorId]) => generatorId);
    if (activeGeneratorIds.length === 0) {
      return;
    }
    const timer = window.setTimeout(() => {
      void Promise.all(activeGeneratorIds.map(refreshGenerationRuns)).catch(
        (error: unknown) => {
          setGeneratorError(
            `刷新生成运行状态失败: ${error instanceof Error ? error.message : String(error)}`,
          );
        },
      );
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [generationRuns, refreshGenerationRuns]);

  return useMemo(() => ({
    generators,
    generationRuns,
    generatorError,
    createGenerator,
    refreshGenerationRuns,
    runGenerator,
    updateGenerator,
    deleteGenerator,
    previewGenerator,
  }), [
    createGenerator,
    deleteGenerator,
    generationRuns,
    generatorError,
    generators,
    previewGenerator,
    refreshGenerationRuns,
    runGenerator,
    updateGenerator,
  ]);
}

export type SessionGeneratorResourcesController = ReturnType<
  typeof useSessionGeneratorResources
>;
