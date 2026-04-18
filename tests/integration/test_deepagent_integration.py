#!/usr/bin/env python3
"""
真实DeepAgent集成测试
使用真实KILO API密钥进行实际LLM调用测试
"""
import os
import sys
sys.path.insert(0, os.path.abspath('.'))

# 加载环境变量
import dotenv
dotenv.load_dotenv()

# 验证KILO_API_KEY是否存在
api_key = os.getenv("KILO_API_KEY")
if not api_key:
    print("❌ 错误：未找到KILO_API_KEY，请在.env文件中配置")
    sys.exit(1)

print(f"✅ 找到KILO_API_KEY，长度: {len(api_key)}")

from app.services.agent_execution_service import AgentExecutionService
import asyncio


async def test_real_deepagent():
    print("\n=== 测试真实DeepAgent集成 ===")
    
    # 第一次调用
    print("\n▶️  第一次调用...")
    response1 = await AgentExecutionService.run_step(
        'test_integration_001',
        '你好，请简单介绍一下你自己。'
    )
    
    print(f"\n✅ 第一次响应:")
    print(response1)
    
    # 第二次调用，测试上下文保留
    print("\n▶️  第二次调用（同一会话）...")
    response2 = await AgentExecutionService.run_step(
        'test_integration_001',
        '我刚才问了你什么问题？'
    )
    
    print(f"\n✅ 第二次响应:")
    print(response2)
    
    print("\n🎉 真实DeepAgent集成测试通过！")


if __name__ == "__main__":
    asyncio.run(test_real_deepagent())
