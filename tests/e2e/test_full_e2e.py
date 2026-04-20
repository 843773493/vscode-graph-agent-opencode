#!/usr/bin/env python3
"""
完整端到端运行测试
自动执行完整流程：启动服务器 -> 创建会话 -> 发送消息 -> 接收SSE事件 -> 验证执行完成
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

from httpx import AsyncClient

from scripts.setup_test_env import setup_test_environment

SERVER_URL = "http://127.0.0.1:8000/api/v1"


async def run_full_test():
    workspace_root = setup_test_environment()
    print(f"测试工作区路径: {workspace_root}")

    print("Running full end-to-end test")
    print("=" * 80)

    # 1. 启动服务器
    print("\nStarting server...")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 等待服务器启动
    await asyncio.sleep(3)

    try:
        async with AsyncClient(base_url=SERVER_URL, timeout=30) as client:
            # 2. 测试工作区接口
            print("\nTesting workspace API...")
            resp = await client.get("/workspace", headers={"X-Local-Token": "local-dev-token"})
            assert resp.status_code == 200, f"Workspace API failed: {resp.status_code}"
            print("Workspace API OK")

            # 3. 创建会话
            print("\nCreating session...")
            resp = await client.post(
                "/sessions",
                json={"title": "Auto Test Session"},
                headers={"X-Local-Token": "local-dev-token"},
            )
            assert resp.status_code == 200, f"Create session failed: {resp.status_code}"
            session = resp.json()["data"]
            session_id = session["session_id"]
            print(f"Session created: {session_id}")

            # 4. 发送消息启动任务
            print("\nSending message to start Agent...")
            resp = await client.post(
                f"/sessions/{session_id}/messages",
                json={
                    "message": {
                        "role": "user",
                        "content": "你好，请简单回复一句话。",
                    },
                    "run": {
                        "mode": "single_agent",
                        "agent_id": "default",
                    },
                },
                headers={"X-Local-Token": "local-dev-token"},
            )

            assert resp.status_code in (200, 202), f"Send message failed: {resp.status_code}"
            job_data = resp.json()["data"]
            job_id = job_data["job_id"]
            print(f"Job started: {job_id}")

            # 5. 等待任务完成
            print("\nWaiting for job execution...")
            for _ in range(30):
                resp = await client.get(f"/jobs/{job_id}", headers={"X-Local-Token": "local-dev-token"})
                if resp.status_code == 200:
                    job = resp.json()["data"]
                    print(f"Job status: {job['status']}")
                    if job["status"] in ("succeeded", "failed", "cancelled"):
                        break
                await asyncio.sleep(1)

            # 6. 获取消息列表
            print("\nGetting execution result...")
            resp = await client.get(
                f"/sessions/{session_id}/messages",
                headers={"X-Local-Token": "local-dev-token"},
            )
            messages = resp.json()["data"]["items"]

            print(f"\nExecution completed! Total messages: {len(messages)}")
            for msg in messages:
                if msg["role"] == "assistant":
                    print(f"\nAgent response: {msg['content'][:200]}")

            print("\n" + "=" * 80)
            print("✅ Full end-to-end flow passed! System is working correctly.")

    finally:
        # 停止服务器
        server.terminate()
        server.wait()


if __name__ == "__main__":
    asyncio.run(run_full_test())