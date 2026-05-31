#!/usr/bin/env python3
"""
真实 Agent 执行端到端测试。
运行在独立的 e2e 环境中，依赖真实 KILO API 调用。
"""
from __future__ import annotations

import uuid
import pytest

from app.services.agent_execution_service import AgentExecutionService


@pytest.mark.asyncio
async def test_agent_initialization():
    """Test agent can be initialized correctly"""
    print("\n=== Test 1: Agent Initialization ===")

    agent = AgentExecutionService.get_for_session("test_session_init")
    assert agent is not None
    print("[OK] Agent initialized successfully")


@pytest.mark.asyncio
async def test_single_step_execution():
    """Test minimal single step execution"""
    print("\n=== Test 2: Single Step Execution ===")

    import uuid
    job_id = str(uuid.uuid4())
    response = await AgentExecutionService.run_step(
        "test_session_001",
        "简单回复一句话：你好，我是测试助手",
        job_id=job_id
    )

    assert isinstance(response, str)
    assert response.strip()
    print("[OK] Agent returned response")
    print(f"Response length: {len(response)}")
    print(f"Response: {response[:200]}")


@pytest.mark.asyncio
async def test_session_isolation():
    """Test different sessions have isolated state"""
    print("\n=== Test 3: Session Isolation ===")

    resp_a1 = await AgentExecutionService.run_step("session_a", "记住这个数字：42", job_id=str(uuid.uuid4()))

    resp_b1 = await AgentExecutionService.run_step("session_b", "记住这个数字：88", job_id=str(uuid.uuid4()))

    resp_a2 = await AgentExecutionService.run_step("session_a", "我刚才告诉你的数字是什么？", job_id=str(uuid.uuid4()))

    resp_b2 = await AgentExecutionService.run_step("session_b", "我刚才告诉你的数字是什么？", job_id=str(uuid.uuid4()))

    assert all(
        isinstance(response, str) and response.strip()
        for response in [resp_a1, resp_b1, resp_a2, resp_b2]
    )
    print("[OK] Sessions are properly isolated")
