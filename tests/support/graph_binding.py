"""session-control 单元测试共用的绑定 selector。"""

import json

from app.core.session_control_store.contracts import ThreadGraphBindingRecord

TEST_THREAD_GRAPH_BINDING = ThreadGraphBindingRecord(
    graph_id="test-graph",
    graph_revision=1,
    graph_schema_hash="sha256:" + "a" * 64,
    capability_profile_hash="sha256:" + "b" * 64,
)
TEST_THREAD_GRAPH_BINDING_JSON = json.dumps(
    {
        "graph_id": TEST_THREAD_GRAPH_BINDING.graph_id,
        "graph_revision": TEST_THREAD_GRAPH_BINDING.graph_revision,
        "graph_schema_hash": TEST_THREAD_GRAPH_BINDING.graph_schema_hash,
        "capability_profile_hash": TEST_THREAD_GRAPH_BINDING.capability_profile_hash,
    },
    sort_keys=True,
    separators=(",", ":"),
)
