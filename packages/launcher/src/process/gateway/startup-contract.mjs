function requiredStartupValue(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new TypeError(`${label} 必须是非空字符串`);
  }
  return value.trim();
}

export function gatewayStartupContract(environment = process.env) {
  const candidateRef = environment.BOXTEAM_CONFIG_CANDIDATE_REF?.trim() ?? "";
  const generation = environment.BOXTEAM_CONFIG_GENERATION?.trim() ?? "";
  const fencingToken = environment.BOXTEAM_CONFIG_FENCING_TOKEN?.trim() ?? "";
  if (candidateRef === "") {
    if (generation !== "" || fencingToken !== "") {
      throw new Error(
        "Gateway 普通恢复不能携带不完整的 pending candidate 启动契约",
      );
    }
    return Object.freeze({
      loadedSource: "active",
      candidateRef: null,
      generation: null,
      fencingToken: null,
      environment: Object.freeze({}),
    });
  }
  return Object.freeze({
    loadedSource: "pending",
    candidateRef: requiredStartupValue(candidateRef, "Gateway candidate_ref"),
    generation: requiredStartupValue(generation, "Gateway generation"),
    fencingToken: requiredStartupValue(fencingToken, "Gateway fencing token"),
    environment: Object.freeze({
      BOXTEAM_CONFIG_CANDIDATE_REF: candidateRef,
      BOXTEAM_CONFIG_GENERATION: generation,
      BOXTEAM_CONFIG_FENCING_TOKEN: fencingToken,
    }),
  });
}

export function buildGatewayPendingEnvironment(baseEnvironment, intent) {
  if (intent === null || typeof intent !== "object" || Array.isArray(intent)) {
    throw new TypeError("Gateway pending restart intent 必须是对象");
  }
  return {
    ...baseEnvironment,
    BOXTEAM_CONFIG_CANDIDATE_REF: requiredStartupValue(
      intent.candidate_ref,
      "Gateway candidate_ref",
    ),
    BOXTEAM_CONFIG_GENERATION: requiredStartupValue(
      intent.target_generation,
      "Gateway generation",
    ),
    BOXTEAM_CONFIG_FENCING_TOKEN: requiredStartupValue(
      intent.fencing_token,
      "Gateway fencing token",
    ),
  };
}
