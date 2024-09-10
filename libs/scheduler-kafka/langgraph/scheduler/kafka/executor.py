import asyncio
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from functools import partial
from typing import Any, Self, Sequence

import aiokafka
from langchain_core.runnables import RunnableConfig

import langgraph.scheduler.kafka.serde as serde
from langgraph.constants import ERROR
from langgraph.errors import TaskNotFound
from langgraph.pregel import Pregel
from langgraph.pregel.algo import prepare_single_task
from langgraph.pregel.executor import AsyncBackgroundExecutor, Submit
from langgraph.pregel.manager import AsyncChannelsManager
from langgraph.pregel.runner import PregelRunner
from langgraph.scheduler.kafka.types import (
    MessageToExecutor,
    MessageToOrchestrator,
    Topics,
)


class KafkaExecutor(AbstractAsyncContextManager):
    def __init__(
        self,
        graph: Pregel,
        topics: Topics,
        *,
        group_id: str = "executor",
        batch_max_n: int = 10,
        batch_max_ms: int = 1000,
        **kwargs: Any,
    ) -> None:
        self.graph = graph
        self.topics = topics
        self.stack = AsyncExitStack()
        self.kwargs = kwargs
        self.group_id = group_id
        self.batch_max_n = batch_max_n
        self.batch_max_ms = batch_max_ms

    async def __aenter__(self) -> Self:
        self.consumer = await self.stack.enter_async_context(
            aiokafka.AIOKafkaConsumer(
                self.topics.executor,
                value_deserializer=serde.loads,
                auto_offset_reset="earliest",
                group_id=self.group_id,
                enable_auto_commit=False,
                **self.kwargs,
            )
        )
        self.producer = await self.stack.enter_async_context(
            aiokafka.AIOKafkaProducer(
                value_serializer=serde.dumps,
                **self.kwargs,
            )
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stack.__aexit__(*args)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Sequence[MessageToExecutor]:
        # wait for next batch
        try:
            recs = await self.consumer.getmany(
                timeout_ms=self.batch_max_ms, max_records=self.batch_max_n
            )
            msgs: list[MessageToExecutor] = [
                msg.value for msgs in recs.values() for msg in msgs
            ]
        except aiokafka.ConsumerStoppedError:
            raise StopAsyncIteration from None
        # process batch
        await asyncio.gather(*(self.each(msg) for msg in msgs))
        # commit offsets
        await self.consumer.commit()
        # return message
        return msgs

    async def each(self, msg: MessageToExecutor) -> None:
        # process message
        saved = await self.graph.checkpointer.aget_tuple(msg["config"])
        if saved is None:
            raise RuntimeError("Checkpoint not found")
        async with AsyncChannelsManager(
            self.graph.channels, saved.checkpoint, msg["config"], self.graph.store
        ) as (channels, managed), AsyncBackgroundExecutor() as submit:
            if task := await asyncio.to_thread(
                prepare_single_task,
                msg["task"]["path"],
                msg["task"]["id"],
                checkpoint=saved.checkpoint,
                processes=self.graph.nodes,
                channels=channels,
                managed=managed,
                config=msg["config"],
                step=msg["task"]["step"],
                for_execution=True,
                is_resuming=msg["task"]["resuming"],
            ):
                # execute task, saving writes
                runner = PregelRunner(
                    submit=submit,
                    put_writes=partial(self._put_writes, submit, msg["config"]),
                )
                async for _ in runner.atick([task]):
                    pass
            else:
                # task was not found
                await self.graph.checkpointer.put_writes(
                    msg["config"], [(ERROR, TaskNotFound())]
                )
        # notify orchestrator
        await self.producer.send_and_wait(
            self.topics.orchestrator,
            value=MessageToOrchestrator(input=None, config=msg["config"]),
        )

    def _put_writes(
        self,
        submit: Submit,
        config: RunnableConfig,
        task_id: str,
        writes: list[tuple[str, Any]],
    ) -> None:
        return submit(self.graph.checkpointer.aput_writes, config, writes, task_id)
