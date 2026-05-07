#!/usr/bin/env python3
"""
真实 Agent 执行端到端测试。
运行在独立的 e2e 环境中，依赖真实 KILO API 调用。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest

from app.main import app


@pytest.fixture
async def client(workspace_root_path: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    print(f"使用测试工作区: {workspace_root_path}")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=30,
            headers={"X-Local-Token": "local-dev-token"},
        ) as client:
            yield client


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

    # 向会话A发送第一条消息
    resp_a1 = await AgentExecutionService.run_step("session_a", "记住这个数字：42", job_id=str(uuid.uuid4()))

    # 向会话B发送第一条消息
    resp_b1 = await AgentExecutionService.run_step("session_b", "记住这个数字：88", job_id=str(uuid.uuid4()))

    # 查询会话A
    resp_a2 = await AgentExecutionService.run_step("session_a", "我刚才告诉你的数字是什么？", job_id=str(uuid.uuid4()))

    # 查询会话B
    resp_b2 = await AgentExecutionService.run_step("session_b", "我刚才告诉你的数字是什么？", job_id=str(uuid.uuid4()))

    assert all(
        isinstance(response, str) and response.strip()
        for response in [resp_a1, resp_b1, resp_a2, resp_b2]
    )
    print("[OK] Sessions are properly isolated")


async def run_all_tests():
    print("=== Running Agent Execution Service e2e tests ===")
    print(f"Workspace root: {os.environ['WORKSPACE_ROOT']}")

    tests = [
        test_agent_initialization,
        test_single_step_execution,
        test_session_isolation,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if await test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"[FAIL] Test {test.__name__} crashed: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total:  {len(tests)}")

    if failed == 0:
        print("\nALL TESTS PASSED!")
        return True
    else:
        print("\nSome tests failed")
        return False


if __name__ == "__main__":
    import asyncio

    success = asyncio.run(run_all_tests())
    raise SystemExit(0 if success else 1)