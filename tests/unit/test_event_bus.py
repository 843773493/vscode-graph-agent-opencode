import asyncio
import uuid
import pytest
from app.core.event_bus import EventBus, EventType
from app.schemas.job import EventDTO


class TestEventBus:
    """测试事件总线功能"""

    def setup_method(self):
        """每个测试前重置事件总线单例"""
        EventBus._instance = None
        self.bus = EventBus.get_instance()
        self.job_id = f"test-job-{uuid.uuid4().hex[:8]}"

    @pytest.mark.asyncio
    async def test_publish_subscribe_basic(self):
        """测试基本的事件发布订阅"""
        queue = await self.bus.subscribe(self.job_id)
        
        # 发布事件
        test_payload = {"message": "test event"}
        event = await self.bus.publish(self.job_id, EventType.LOG, test_payload)
        
        # 验证事件被接收
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received.event_id == event.event_id
        assert received.type == EventType.LOG
        assert received.payload == test_payload
        assert received.job_id == self.job_id

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        """测试多个客户端订阅同一Job事件"""
        queue1 = await self.bus.subscribe(self.job_id)
        queue2 = await self.bus.subscribe(self.job_id)
        queue3 = await self.bus.subscribe(self.job_id)
        
        # 发布事件
        event = await self.bus.publish(self.job_id, EventType.AGENT_START, {})
        
        # 所有订阅者都应该收到事件
        received1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
        received2 = await asyncio.wait_for(queue2.get(), timeout=1.0)
        received3 = await asyncio.wait_for(queue3.get(), timeout=1.0)
        
        assert received1.event_id == event.event_id
        assert received2.event_id == event.event_id
        assert received3.event_id == event.event_id

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """测试取消订阅"""
        queue = await self.bus.subscribe(self.job_id)
        
        # 发布第一个事件
        await self.bus.publish(self.job_id, EventType.LOG, {"msg": "1"})
        received = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert received is not None
        
        # 取消订阅
        await self.bus.unsubscribe(self.job_id, queue)
        
        # 发布第二个事件，应该收不到
        await self.bus.publish(self.job_id, EventType.LOG, {"msg": "2"})
        
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_job_isolation(self):
        """测试不同Job之间事件隔离"""
        job1 = "job-1"
        job2 = "job-2"
        
        queue1 = await self.bus.subscribe(job1)
        queue2 = await self.bus.subscribe(job2)
        
        # 向job1发布事件
        await self.bus.publish(job1, EventType.LOG, {"for": "job1"})
        
        # job1收到，job2收不到
        received1 = await asyncio.wait_for(queue1.get(), timeout=1.0)
        assert received1.payload["for"] == "job1"
        
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue2.get(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_ring_buffer_history(self):
        """测试环形缓冲区历史记录"""
        # 发布超过缓冲区大小的事件
        total_events = 1500
        for i in range(total_events):
            await self.bus.publish(self.job_id, EventType.LOG, {"index": i})
        
        # 获取历史事件
        events = await self.bus.list_events(self.job_id, limit=2000)
        
        # 应该只保留最新的1000个事件
        assert len(events) == 1000
        assert events[0].payload["index"] == 500  # 第一个保留的是第500个
        assert events[-1].payload["index"] == 1499  # 最后一个是第1499个

    @pytest.mark.asyncio
    async def test_list_events_after_cursor(self):
        """测试按游标获取后续事件"""
        # 发布多个事件
        events = []
        for i in range(10):
            evt = await self.bus.publish(self.job_id, EventType.LOG, {"index": i})
            events.append(evt)
        
        # 获取第5个事件之后的事件
        after_id = events[4].event_id
        result = await self.bus.list_events(self.job_id, after=after_id)
        
        assert len(result) == 5
        assert result[0].event_id == events[5].event_id
        assert result[-1].event_id == events[9].event_id

    @pytest.mark.asyncio
    async def test_list_events_limit(self):
        """测试事件列表限制"""
        for i in range(50):
            await self.bus.publish(self.job_id, EventType.LOG, {"index": i})
        
        result = await self.bus.list_events(self.job_id, limit=10)
        assert len(result) == 10
        assert result[0].payload["index"] == 40
        assert result[-1].payload["index"] == 49

    @pytest.mark.asyncio
    async def test_subscriber_queue_full(self):
        """测试订阅者队列满时不阻塞发布"""
        queue = await self.bus.subscribe(self.job_id)
        
        # 填满队列 (maxsize=100)
        for i in range(100):
            await self.bus.publish(self.job_id, EventType.LOG, {"index": i})
        
        # 发布更多事件，应该不会阻塞，旧事件会被丢弃
        await self.bus.publish(self.job_id, EventType.LOG, {"index": 100})
        await self.bus.publish(self.job_id, EventType.LOG, {"index": 101})
        
        # 验证队列仍然只有100个事件
        count = 0
        while not queue.empty():
            await queue.get()
            count += 1
        
        assert count == 100

    @pytest.mark.asyncio
    async def test_event_metadata(self):
        """测试事件元数据正确性"""
        step_id = "step-123"
        agent_id = "agent-456"
        
        event = await self.bus.publish(
            self.job_id, 
            EventType.TOOL_CALL, 
            {"tool": "git"},
            step_id=step_id,
            agent_id=agent_id
        )
        
        assert event.step_id == step_id
        assert event.agent_id == agent_id
        assert event.event_id.startswith("evt_")
        assert len(event.event_id) == 16  # evt_ + 12 hex chars
        assert event.timestamp is not None

    @pytest.mark.asyncio
    async def test_concurrent_publish(self):
        """测试并发发布事件"""
        async def publish_events(count: int, start: int):
            for i in range(start, start + count):
                await self.bus.publish(self.job_id, EventType.LOG, {"i": i})
        
        # 启动多个并发发布任务
        tasks = [
            asyncio.create_task(publish_events(50, 0)),
            asyncio.create_task(publish_events(50, 50)),
            asyncio.create_task(publish_events(50, 100)),
        ]
        
        await asyncio.gather(*tasks)
        
        events = await self.bus.list_events(self.job_id, limit=200)
        assert len(events) == 150

    def test_singleton_instance(self):
        """测试事件总线是单例"""
        bus1 = EventBus.get_instance()
        bus2 = EventBus.get_instance()
        assert bus1 is bus2
