from __future__ import annotations

import asyncio
import concurrent.futures
from collections import defaultdict, deque
from functools import partial
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Type,
    Union,
    cast,
    overload,
)

from langchain.callbacks.manager import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain.globals import get_debug
from langchain.pydantic_v1 import BaseModel, Field, create_model, root_validator
from langchain.schema.runnable import (
    Runnable,
    RunnableSerializable,
)
from langchain.schema.runnable.base import Input, Output, coerce_to_runnable
from langchain.schema.runnable.config import (
    RunnableConfig,
    ensure_config,
    get_executor_for_config,
    patch_config,
)
from langchain.schema.runnable.utils import (
    ConfigurableFieldSpec,
    get_unique_config_specs,
)

from permchain.channels.base import (
    AsyncChannelsManager,
    BaseChannel,
    ChannelsManager,
    EmptyChannelError,
    create_checkpoint,
)
from permchain.checkpoint.base import (
    BaseCheckpointAdapter,
    Checkpoint,
    CheckpointAt,
    StepState,
    empty_checkpoint,
)
from permchain.checkpoint.memory import MemoryCheckpoint
from permchain.constants import CONFIG_KEY_READ, CONFIG_KEY_SEND
from permchain.pregel.debug import print_checkpoint, print_step_start
from permchain.pregel.io import map_input, map_output
from permchain.pregel.log import logger
from permchain.pregel.read import ChannelBatch, ChannelInvoke
from permchain.pregel.reserved import ReservedChannels
from permchain.pregel.validate import validate_chains_channels
from permchain.pregel.write import ChannelWrite

WriteValue = Union[
    Runnable[Input, Output],
    Callable[[Input], Output],
    Callable[[Input], Awaitable[Output]],
    Any,
]


def _coerce_write_value(value: WriteValue) -> Runnable[Input, Output]:
    if not isinstance(value, Runnable) and not callable(value):
        return coerce_to_runnable(lambda _: value)
    return coerce_to_runnable(value)


class Channel:
    @overload
    @classmethod
    def subscribe_to(cls, channels: str, key: Optional[str] = None) -> ChannelInvoke:
        ...

    @overload
    @classmethod
    def subscribe_to(cls, channels: Sequence[str], key: None = None) -> ChannelInvoke:
        ...

    @classmethod
    def subscribe_to(
        cls, channels: str | Sequence[str], key: Optional[str] = None
    ) -> ChannelInvoke:
        """Runs process.invoke() each time channels are updated,
        with a dict of the channel values as input."""
        if not isinstance(channels, str) and key is not None:
            raise ValueError(
                "Can't specify a key when subscribing to multiple channels"
            )
        return ChannelInvoke(
            channels=cast(
                Mapping[None, str] | Mapping[str, str],
                {key: channels}
                if isinstance(channels, str)
                else {chan: chan for chan in channels},
            ),
            triggers=[channels] if isinstance(channels, str) else channels,
        )

    @classmethod
    def subscribe_to_each(cls, inbox: str, key: Optional[str] = None) -> ChannelBatch:
        """Runs process.batch() with the content of inbox each time it is updated."""
        return ChannelBatch(channel=inbox, key=key)

    @classmethod
    def write_to(
        cls,
        *channels: str,
        **kwargs: WriteValue,
    ) -> ChannelWrite:
        """Writes to channels the result of the lambda, or None to skip writing."""
        return ChannelWrite(
            channels=(
                [(c, None) for c in channels]
                + [(k, _coerce_write_value(v)) for k, v in kwargs.items()]
            )
        )


class Pregel(RunnableSerializable[dict[str, Any] | Any, dict[str, Any] | Any]):
    chains: Mapping[str, ChannelInvoke | ChannelBatch]

    channels: Mapping[str, BaseChannel] = Field(default_factory=dict)

    output: str | Sequence[str] = "output"

    input: str | Sequence[str] = "input"

    step_timeout: Optional[float] = None

    debug: bool = Field(default_factory=get_debug)

    saver: Optional[BaseCheckpointAdapter] = None

    class Config:
        arbitrary_types_allowed = True

    @root_validator(skip_on_failure=True)
    def validate_pregel(cls, values: dict[str, Any]) -> dict[str, Any]:
        validate_chains_channels(
            values["chains"], values["channels"], values["input"], values["output"]
        )
        return values

    @property
    def config_specs(self) -> list[ConfigurableFieldSpec]:
        return get_unique_config_specs(
            [spec for chain in self.chains.values() for spec in chain.config_specs]
            + (self.saver.config_specs if self.saver is not None else [])
        )

    @property
    def InputType(self) -> Any:
        if isinstance(self.input, str):
            return self.channels[self.input].UpdateType

    def get_input_schema(
        self, config: Optional[RunnableConfig] = None
    ) -> Type[BaseModel]:
        if isinstance(self.input, str):
            return super().get_input_schema(config)
        else:
            return create_model(  # type: ignore[call-overload]
                "PregelInput",
                **{
                    k: (self.channels[k].UpdateType, None)
                    for k in self.input or self.channels.keys()
                },
            )

    @property
    def OutputType(self) -> Any:
        if isinstance(self.output, str):
            return self.channels[self.output].ValueType

    def get_output_schema(
        self, config: Optional[RunnableConfig] = None
    ) -> Type[BaseModel]:
        if isinstance(self.output, str):
            return super().get_output_schema(config)
        else:
            return create_model(  # type: ignore[call-overload]
                "PregelOutput",
                **{k: (self.channels[k].ValueType, None) for k in self.output},
            )

    def _transform(
        self,
        input: Iterator[dict[str, Any] | Any],
        run_manager: CallbackManagerForChainRun,
        config: RunnableConfig,
        *,
        saver: Optional[BaseCheckpointAdapter] = None,
    ) -> Iterator[dict[str, Any] | Any]:
        if config["recursion_limit"] < 1:
            raise ValueError("recursion_limit must be at least 1")
        processes = {**self.chains}
        saver = saver or self.saver
        checkpoint = (
            self.saver.get(config) if self.saver else None
        ) or empty_checkpoint()
        with ChannelsManager(
            self.channels, checkpoint
        ) as channels, get_executor_for_config(config) as executor:
            _apply_writes(
                checkpoint,
                channels,
                deque(w for c in input for w in map_input(self.input, c)),
                config,
                0,
            )

            read = partial(_read_channel, channels)

            # Similarly to Bulk Synchronous Parallel / Pregel model
            # computation proceeds in steps, while there are channel updates
            # channel updates from step N are only visible in step N+1
            # channels are guaranteed to be immutable for the duration of the step,
            # with channel updates applied only at the transition between steps
            for step in range(config["recursion_limit"]):
                next_tasks = _prepare_next_tasks(checkpoint, processes, channels)

                # if no more tasks, we're done
                if not next_tasks:
                    break

                if self.debug:
                    print_step_start(step, next_tasks)

                # collect all writes to channels, without applying them yet
                pending_writes = deque[tuple[str, Any]]()

                # execute tasks, and wait for one to fail or all to finish.
                # each task is independent from all other concurrent tasks
                done, inflight = concurrent.futures.wait(
                    [
                        executor.submit(
                            proc.invoke,
                            input,
                            patch_config(
                                config,
                                callbacks=run_manager.get_child(f"pregel:step:{step}"),
                                configurable={
                                    # deque.extend is thread-safe
                                    CONFIG_KEY_SEND: pending_writes.extend,
                                    CONFIG_KEY_READ: read,
                                },
                            ),
                        )
                        for proc, input, _ in next_tasks
                    ],
                    return_when=concurrent.futures.FIRST_EXCEPTION,
                    timeout=self.step_timeout,
                )

                # interrupt on failure or timeout
                _interrupt_or_proceed(done, inflight, step)

                # apply writes to channels
                _apply_writes(checkpoint, channels, pending_writes, config, step + 1)

                if self.debug:
                    print_checkpoint(step, channels)

                # if any write to output channels in this step, yield current value
                for output in map_output(self.output, pending_writes, channels):
                    yield output

                # save end of step checkpoint
                if self.saver is not None and self.saver.at == CheckpointAt.END_OF_STEP:
                    checkpoint = create_checkpoint(checkpoint, channels)
                    self.saver.put(config, checkpoint)

            # save end of run checkpoint
            if self.saver is not None and self.saver.at == CheckpointAt.END_OF_RUN:
                checkpoint = create_checkpoint(checkpoint, channels)
                self.saver.put(config, checkpoint)

    async def _atransform(
        self,
        input: AsyncIterator[dict[str, Any] | Any],
        run_manager: AsyncCallbackManagerForChainRun,
        config: RunnableConfig,
        *,
        saver: Optional[BaseCheckpointAdapter] = None,
    ) -> AsyncIterator[dict[str, Any] | Any]:
        if config["recursion_limit"] < 1:
            raise ValueError("recursion_limit must be at least 1")
        processes = {**self.chains}
        saver = saver or self.saver
        checkpoint = (
            await self.saver.aget(config) if self.saver else None
        ) or empty_checkpoint()
        async with AsyncChannelsManager(self.channels, checkpoint) as channels:
            next_tasks = _apply_writes(
                processes,
                channels,
                deque([w async for c in input for w in map_input(self.input, c)]),
                config,
                0,
            )

            if not next_tasks:
                return

            read = partial(_read_channel, channels)

            # Similarly to Bulk Synchronous Parallel / Pregel model
            # computation proceeds in steps, while there are channel updates
            # channel updates from step N are only visible in step N+1,
            # channels are guaranteed to be immutable for the duration of the step,
            # channel updates being applied only at the transition between steps
            for step in range(config["recursion_limit"]):
                if self.debug:
                    print_step_start(step, next_tasks)

                # collect all writes to channels, without applying them yet
                pending_writes = deque[tuple[str, Any]]()

                # execute tasks, and wait for one to fail or all to finish.
                # each task is independent from all other concurrent tasks
                done, inflight = await asyncio.wait(
                    [
                        asyncio.create_task(
                            proc.ainvoke(
                                input,
                                patch_config(
                                    config,
                                    callbacks=run_manager.get_child(
                                        f"pregel:step:{step}"
                                    ),
                                    configurable={
                                        # deque.extend is thread-safe
                                        CONFIG_KEY_SEND: pending_writes.extend,
                                        CONFIG_KEY_READ: read,
                                    },
                                ),
                            )
                        )
                        for proc, input, _ in next_tasks
                    ],
                    return_when=asyncio.FIRST_EXCEPTION,
                    timeout=self.step_timeout,
                )

                # interrupt on failure or timeout
                _interrupt_or_proceed(done, inflight, step)

                # apply writes to channels, decide on next step
                next_tasks = _apply_writes(
                    processes, channels, pending_writes, config, step + 1
                )

                if self.debug:
                    print_checkpoint(step, channels)

                # if any write to output channels in this step, yield current value
                for output in map_output(self.output, pending_writes, channels):
                    yield output

                # save end of step checkpoint
                if (
                    checkpointer is not None
                    and checkpointer.at == CheckpointAt.END_OF_STEP
                ):
                    checkpoint = create_checkpoint(channels)
                    await checkpointer.aput(config, checkpoint)

                # if no more tasks, we're done
                if not next_tasks:
                    break

            # save end of run checkpoint
            if checkpointer is not None and checkpointer.at == CheckpointAt.END_OF_RUN:
                checkpoint = create_checkpoint(channels)
                await checkpointer.aput(config, checkpoint)

    def invoke(
        self,
        input: dict[str, Any] | Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        latest: dict[str, Any] | Any = None
        for chunk in self.stream(input, config, **kwargs):
            latest = chunk
        return latest

    def stream(
        self,
        input: dict[str, Any] | Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any] | Any]:
        return self.transform(iter([input]), config, **kwargs)

    def transform(
        self,
        input: Iterator[dict[str, Any] | Any],
        config: RunnableConfig | None = None,
        **kwargs: Any | None,
    ) -> Iterator[dict[str, Any] | Any]:
        return self._transform_stream_with_config(
            input, self._transform, config, **kwargs
        )

    def step(
        self,
        input: dict[str, Any] | Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Iterator[StepState]:
        config = ensure_config(config)
        recursion_limit = config["recursion_limit"]
        config = patch_config(config, recursion_limit=1)
        step_checkpointer = MemoryCheckpoint()
        global_checkpointer = self.saver
        for i in range(recursion_limit):
            for chunk in self.stream(
                input, config, checkpointer=step_checkpointer, **kwargs
            ):
                checkpoint = step_checkpointer.get(config)
                yield StepState(checkpoint=checkpoint)

    async def ainvoke(
        self,
        input: dict[str, Any] | Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | Any:
        latest: dict[str, Any] | Any = None
        async for chunk in self.astream(input, config, **kwargs):
            latest = chunk
        return latest

    async def astream(
        self,
        input: dict[str, Any] | Any,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any] | Any]:
        async def input_stream() -> AsyncIterator[dict[str, Any] | Any]:
            yield input

        async for chunk in self.atransform(input_stream(), config, **kwargs):
            yield chunk

    async def atransform(
        self,
        input: AsyncIterator[dict[str, Any] | Any],
        config: RunnableConfig | None = None,
        **kwargs: Any | None,
    ) -> AsyncIterator[dict[str, Any] | Any]:
        async for chunk in self._atransform_stream_with_config(
            input, self._atransform, config, **kwargs
        ):
            yield chunk


def _interrupt_or_proceed(
    done: set[concurrent.futures.Future[Any]] | set[asyncio.Task[Any]],
    inflight: set[concurrent.futures.Future[Any]] | set[asyncio.Task[Any]],
    step: int,
) -> None:
    while done:
        # if any task failed
        if exc := done.pop().exception():
            # cancel all pending tasks
            while inflight:
                inflight.pop().cancel()
            # raise the exception
            raise exc
            # TODO this is where retry of an entire step would happen

    if inflight:
        # if we got here means we timed out
        while inflight:
            # cancel all pending tasks
            inflight.pop().cancel()
        # raise timeout error
        raise TimeoutError(f"Timed out at step {step}")


def _read_channel(
    channels: Mapping[str, BaseChannel], chan: str, catch: bool = True
) -> Any:
    try:
        return channels[chan].get()
    except EmptyChannelError:
        return None


def _apply_writes(
    checkpoint: Checkpoint,
    channels: Mapping[str, BaseChannel],
    pending_writes: Sequence[tuple[str, Any]],
    config: RunnableConfig,
    for_step: int,
) -> None:
    pending_writes_by_channel: dict[str, list[Any]] = defaultdict(list)
    # Group writes by channel
    for chan, val in pending_writes:
        if chan in [c.value for c in ReservedChannels]:
            raise ValueError(f"Can't write to reserved channel {chan}")
        pending_writes_by_channel[chan].append(val)

    # Update reserved channels
    pending_writes_by_channel[ReservedChannels.is_last_step] = [
        for_step + 1 == config["recursion_limit"]
    ]

    updated_channels: set[str] = set()
    # Apply writes to channels
    for chan, vals in pending_writes_by_channel.items():
        if chan in channels:
            channels[chan].update(vals)
            checkpoint["versions"][chan] += 1
            updated_channels.add(chan)
        else:
            logger.warning(f"Skipping write for channel {chan} which has no readers")
    # Channels that weren't updated in this step are notified of a new step
    for chan in channels:
        if chan not in updated_channels:
            channels[chan].update([])


def _prepare_next_tasks(
    checkpoint: Checkpoint,
    processes: Mapping[str, ChannelInvoke | ChannelBatch],
    channels: Mapping[str, BaseChannel],
) -> list[tuple[Runnable, Any, str]]:
    tasks: list[tuple[Runnable, Any, str]] = []
    # Check if any processes should be run in next step
    # If so, prepare the values to be passed to them
    for name, proc in processes.items():
        seen = checkpoint["seen"][name]
        if isinstance(proc, ChannelInvoke):
            # If any of the channels read by this process were updated
            if any(checkpoint["versions"][chan] > seen[chan] for chan in proc.triggers):
                # If all channels subscribed by this process have been initialized
                try:
                    val = {
                        k: _read_channel(
                            channels, chan, catch=chan not in proc.triggers
                        )
                        for k, chan in proc.channels.items()
                    }
                except EmptyChannelError:
                    continue

                # Processes that subscribe to a single keyless channel get
                # the value directly, instead of a dict
                if list(proc.channels.keys()) == [None]:
                    val = val[None]

                tasks.append((proc, val, name))
                seen.update(
                    {chan: checkpoint["versions"][chan] for chan in proc.triggers}
                )
        elif isinstance(proc, ChannelBatch):
            # If the channel read by this process was updated
            if checkpoint["versions"][proc.channel] > seen[proc.channel]:
                # Here we don't catch EmptyChannelError because the channel
                # must be intialized if the previous `if` condition is true
                val = channels[proc.channel].get()
                if proc.key is not None:
                    val = [{proc.key: v} for v in val]

                tasks.append((proc, val, name))
                seen[proc.channel] = checkpoint["versions"][proc.channel]

    return tasks
