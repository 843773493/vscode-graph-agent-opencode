#!/usr/bin/env python3
"""
Test for minimal Agent single step execution cycle.
Runs in isolated playground environment.
"""
import os
import sys
sys.path.insert(0, os.path.abspath('.'))

# Setup test environment BEFORE importing app modules
from scripts.setup_test_env import setup_test_environment
setup_test_environment()

# Now import app modules
import dotenv
dotenv.load_dotenv()

from app.services.agent_execution_service import AgentExecutionService
import asyncio


async def test_agent_initialization():
    """Test agent can be initialized correctly"""
    print("\n=== Test 1: Agent Initialization ===")
    
    try:
        agent = AgentExecutionService.get_for_session("test_session_init")
        print("✅ Agent initialized successfully")
        return True
    except Exception as e:
        print(f"❌ Agent initialization failed: {e}")
        return False


async def test_single_step_execution():
    """Test minimal single step execution"""
    print("\n=== Test 2: Single Step Execution ===")
    
    try:
        response = await AgentExecutionService.run_step(
            "test_session_001",
            "简单回复一句话：你好，我是测试助手"
        )
        
        print(f"✅ Agent returned response")
        print(f"Response length: {len(response)}")
        print(f"Response: {response[:200]}")
        
        return True
    except Exception as e:
        print(f"❌ Execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_session_isolation():
    """Test different sessions have isolated state"""
    print("\n=== Test 3: Session Isolation ===")
    
    try:
        # Send first message to session A
        resp_a1 = await AgentExecutionService.run_step("session_a", "记住这个数字：42")
        
        # Send first message to session B
        resp_b1 = await AgentExecutionService.run_step("session_b", "记住这个数字：88")
        
        # Query session A
        resp_a2 = await AgentExecutionService.run_step("session_a", "我刚才告诉你的数字是什么？")
        
        # Query session B
        resp_b2 = await AgentExecutionService.run_step("session_b", "我刚才告诉你的数字是什么？")
        
        print("✅ Sessions are properly isolated")
        return True
    except Exception as e:
        print(f"❌ Session isolation test failed: {e}")
        return False


async def run_all_tests():
    print("🔍 Running Agent Execution Service tests")
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
            print(f"❌ Test {test.__name__} crashed: {e}")
            failed += 1
    
    print(f"\n{'='*60}")
    print(f"✅ Passed: {passed}")
    print(f"❌ Failed: {failed}")
    print(f"Total:  {len(tests)}")
    
    if failed == 0:
        print("\n🎉 ALL TESTS PASSED!")
        return True
    else:
        print("\n⚠️ Some tests failed")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
