from collections import deque
from contextlib import ExitStack
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from langchain_core.runnables import RunnableConfig
from typing_extensions import Self

from langgraph.channels.base import BaseChannel
from langgraph.channels.manager import ChannelsManager, create_checkpoint
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    copy_checkpoint,
    empty_checkpoint,
)
from langgraph.constants import INTERRUPT
from langgraph.managed.base import ManagedValueMapping, ManagedValuesManager
from langgraph.pregel.algo import (
    apply_writes,
    increment,
    prepare_next_tasks,
    should_interrupt,
)
from langgraph.pregel.debug import map_debug_checkpoint
from langgraph.pregel.executor import BackgroundExecutor, Submit
from langgraph.pregel.io import map_input, map_output_updates, map_output_values, single
from langgraph.pregel.types import PregelExecutableTask

if TYPE_CHECKING:
    from langgraph.pregel import Pregel


V = TypeVar("V")
INPUT_DONE = object()


class PregelLoop(ContextManager):
    input: Optional[Any]
    config: RunnableConfig
    checkpointer: Optional[BaseCheckpointSaver]
    get_next_version: Callable[[Optional[V]], V]
    graph: "Pregel"

    submit: Submit
    channels: Mapping[str, BaseChannel]
    managed: ManagedValueMapping
    checkpoint: Checkpoint
    checkpoint_config: RunnableConfig
    checkpoint_metadata: CheckpointMetadata
    checkpoint_pending_writes: Optional[List[PendingWrite]]

    status: Literal[
        "pending", "done", "interrupt_before", "interrupt_after", "out_of_steps"
    ]
    tasks: Sequence[PregelExecutableTask]
    stream: deque[Tuple[str, Any]]

    def __init__(
        self,
        input: Optional[Any],
        *,
        config: RunnableConfig,
        checkpointer: Optional[BaseCheckpointSaver],
        graph: "Pregel",
    ) -> None:
        self.stream = deque()
        self.stack = ExitStack()
        self.input = input
        self.config = config
        self.checkpointer = checkpointer
        self.get_next_version = (
            checkpointer.get_next_version if checkpointer else increment
        )
        self.graph = graph
        # TODO if managed values no longer needs graph we can replace with
        # managed_specs, channel_specs

    def __enter__(self) -> Self:
        saved = (
            self.checkpointer.get_tuple(self.config) if self.checkpointer else None
        ) or CheckpointTuple(self.config, empty_checkpoint(), {"step": -2}, None, [])
        self.checkpoint_config = {
            **self.config,
            **saved.config,
            "configurable": {
                **self.config.get("configurable", {}),
                **saved.config.get("configurable", {}),
            },
        }
        self.checkpoint = saved.checkpoint
        self.checkpoint_metadata = saved.metadata
        self.checkpoint_pending_writes = saved.pending_writes

        self.submit = self.stack.enter_context(BackgroundExecutor(self.config))
        self.channels = self.stack.enter_context(
            ChannelsManager(self.graph.channels, self.checkpoint, self.config)
        )
        self.managed = self.stack.enter_context(
            ManagedValuesManager(
                self.graph.managed_values_dict, self.config, self.graph
            )
        )
        self.status = "pending"
        self.step = self.checkpoint_metadata["step"] + 1

        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        del self.graph
        return self.stack.__exit__(exc_type, exc_value, traceback)

    def tick(
        self,
        *,
        output_keys: Union[str, Sequence[str]] = None,
        interrupt_after: Optional[Sequence[str]] = None,
        interrupt_before: Optional[Sequence[str]] = None,
    ) -> bool:
        if self.status != "pending":
            raise RuntimeError(f"Cannot tick when status is {self.status}")
        if self.input is not INPUT_DONE:
            self.first()
        elif len({tid for tid, _, _ in self.checkpoint_pending_writes}) == len(
            self.tasks
        ):
            # assign writes to tasks, apply them in order
            grouped: dict[str, list[tuple[str, Any]]] = {}
            for tid, k, v in self.checkpoint_pending_writes:
                grouped.setdefault(tid, []).append((k, v))
            writes = [(k, v) for t in self.tasks for k, v in grouped.get(t.id, [])]
            # all tasks have finished
            apply_writes(
                self.checkpoint,
                self.channels,
                writes,
                self.get_next_version,
            )
            # produce values output
            self.stream.extend(
                ("values", v)
                for v in map_output_values(output_keys, writes, self.channels)
            )
            # clear pending writes
            self.checkpoint_pending_writes.clear()
            # save checkpoint
            self.put_checkpoint(
                {
                    "source": "loop",
                    "writes": single(
                        map_output_updates(output_keys, self.tasks)
                        if self.graph.stream_mode == "updates"
                        else map_output_values(output_keys, writes, self.channels)
                    ),
                }
            )
            # after execution, check if we should interrupt
            if should_interrupt(self.checkpoint, interrupt_after, self.tasks):
                self.status = "interrupt_after"
                return False
        else:
            return False

        # check if iteration limit is reached
        if self.step > self.config["recursion_limit"]:
            self.status = "out_of_steps"
            return False

        # prepare next tasks
        prev_checkpoint = self.checkpoint
        self.checkpoint, self.tasks = prepare_next_tasks(
            self.checkpoint,
            self.graph.nodes,
            self.channels,
            self.managed,
            self.config,
            self.step,
            for_execution=True,
            get_next_version=self.get_next_version,
        )

        # if no more tasks, we're done
        if not self.tasks:
            self.status = "done"
            return False

        # TODO how to make this work for both
        # - online case: we should schedule remaining tasks
        # - offline case: we should just bail, as other tasks were scheduled before
        # if there are pending writes from a previous loop, apply them
        if self.checkpoint_pending_writes:
            for tid, k, v in self.checkpoint_pending_writes:
                if task := next((t for t in self.tasks if t.id == tid), None):
                    task.writes.append((k, v))

        # before execution, check if we should interrupt
        if should_interrupt(prev_checkpoint, interrupt_before, self.tasks):
            self.status = "interrupt_before"
            return False

        return True

    def first(self) -> None:
        # map inputs to channel updates
        if input_writes := deque(map_input(self.graph.input_channels, self.input)):
            # discard any unfinished tasks from previous checkpoint
            self.checkpoint, _ = prepare_next_tasks(
                self.checkpoint,
                self.graph.nodes,
                self.channels,
                self.managed,
                self.config,
                self.step,
                for_execution=True,
                get_next_version=self.get_next_version,
                # TODO missing run_manager
            )
            # apply input writes
            apply_writes(
                self.checkpoint,
                self.channels,
                input_writes,
                self.get_next_version,
            )
            # save input checkpoint
            self.put_checkpoint({"source": "input", "writes": self.input})
        else:
            # no input is taken as signal to proceed past previous interrupt
            self.checkpoint = copy_checkpoint(self.checkpoint)
            for k in self.channels:
                if k in self.checkpoint["channel_versions"]:
                    version = self.checkpoint["channel_versions"][k]
                    self.checkpoint["versions_seen"][INTERRUPT][k] = version
        # done with input
        self.input = INPUT_DONE

    def put_writes(self, task_id: str, writes: Sequence[tuple[str, Any]]) -> None:
        self.checkpoint_pending_writes.extend((task_id, k, v) for k, v in writes)
        if self.checkpointer is not None:
            self.submit(
                self.checkpointer.put_writes,
                {
                    **self.checkpoint_config,
                    "configurable": {
                        **self.checkpoint_config["configurable"],
                        "thread_ts": self.checkpoint["id"],
                    },
                },
                writes,
                task_id,
            )

    def put_checkpoint(
        self,
        metadata: CheckpointMetadata,
    ) -> None:
        # assign step
        metadata["step"] = self.step
        # bail if no checkpointer
        if self.checkpointer is not None:
            # create new checkpoint
            self.checkpoint_metadata = metadata
            self.checkpoint = create_checkpoint(
                self.checkpoint, self.channels, self.step
            )
            # save it, without blocking
            self.submit(
                self.checkpointer.put,
                self.checkpoint_config,
                copy_checkpoint(self.checkpoint),
                self.checkpoint_metadata,
            )
            self.checkpoint_config = {
                **self.checkpoint_config,
                "configurable": {
                    **self.checkpoint_config["configurable"],
                    "thread_ts": self.checkpoint["id"],
                },
            }
            # produce debug output
            self.stream.extend(
                ("debug", v)
                for v in map_debug_checkpoint(
                    self.step,
                    self.checkpoint_config,
                    self.channels,
                    self.graph.stream_channels_asis,
                    self.checkpoint_metadata,
                )
            )
        # increment step
        self.step += 1
