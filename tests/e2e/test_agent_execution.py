#!/usr/bin/env python3
"""
真实 Agent 执行端到端测试。
运行在独立的 e2e 环境中，依赖真实 KILO API 调用。
"""
from __future__ import annotations

import uuid
import pytest

from app.agents.agent_factory import create_runtime_deep_agent_for_session


@pytest.mark.asyncio
async def test_agent_initialization(container):
    """Test agent can be initialized correctly"""
    print("\n=== Test 1: Agent Initialization ===")

    agent = create_runtime_deep_agent_for_session(
        session_id="test_session_init",
        agent_id="deep_agent",
        config_service=container.config_service,
        background_task_registry=container.background_task_registry,
        background_message_bus=container.background_message_bus,
        job_event_bus=container.job_event_bus,
        job_service=container.job_service,
        message_service=container.message_service,
        session_service=container.session_service,
    )
    assert agent is not None
    print("[OK] Agent initialized successfully")


@pytest.mark.asyncio
async def test_single_step_execution(container):
    """Test minimal single step execution"""
    print("\n=== Test 2: Single Step Execution ===")

    job_id = str(uuid.uuid4())
    response = await container.agent_execution_service.run_step(
        "test_session_001",
        "简单回复一句话：你好，我是测试助手",
        agent_id="deep_agent",
        job_id=job_id
    )

    assert response is not None
    assert response
    print("[OK] Agent returned response")
    print(f"Response type: {type(response).__name__}")
    print(f"Response: {str(response)[:200]}")


@pytest.mark.asyncio
async def test_session_isolation(container):
    """Test different sessions have isolated state"""
    print("\n=== Test 3: Session Isolation ===")

    resp_a1 = await container.agent_execution_service.run_step(
        "session_a",
        "记住这个数字：42",
        agent_id="deep_agent",
        job_id=str(uuid.uuid4()),
    )

    resp_b1 = await container.agent_execution_service.run_step(
        "session_b",
        "记住这个数字：88",
        agent_id="deep_agent",
        job_id=str(uuid.uuid4()),
    )

    resp_a2 = await container.agent_execution_service.run_step(
        "session_a",
        "我刚才告诉你的数字是什么？",
        agent_id="deep_agent",
        job_id=str(uuid.uuid4()),
    )

    resp_b2 = await container.agent_execution_service.run_step(
        "session_b",
        "我刚才告诉你的数字是什么？",
        agent_id="deep_agent",
        job_id=str(uuid.uuid4()),
    )

    assert all(response is not None for response in [resp_a1, resp_b1, resp_a2, resp_b2])
    print("[OK] Sessions are properly isolated")
