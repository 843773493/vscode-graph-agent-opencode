from __future__ import annotations

COMPACT_HISTORY_PATH_PREFIX = "/.boxteam/conversation_history"
LARGE_TOOL_RESULTS_PATH_PREFIX = "/.boxteam/large_tool_results"


def apply_boxteam_summarization_paths(summarization: object) -> None:
    setattr(summarization, "_history_path_prefix", COMPACT_HISTORY_PATH_PREFIX)
    setattr(summarization, "_large_tool_results_prefix", LARGE_TOOL_RESULTS_PATH_PREFIX)
