import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Generator

import pytest
from langchain.schema.runnable import RunnablePassthrough
from pytest_mock import MockerFixture

from permchain import Pregel, channels


async def test_invoke_single_process_in_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={
            "one": chain,
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    assert await app.ainvoke(2) == 3


async def test_invoke_single_process_in_out_dict(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={
            "one": chain,
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
        },
        input="input",
        output=["output"],
    )

    assert app.input_schema.schema() == {"title": "PregelInput", "type": "integer"}
    assert app.output_schema.schema() == {
        "title": "PregelOutput",
        "type": "object",
        "properties": {"output": {"title": "Output", "type": "integer"}},
    }
    assert await app.ainvoke(2) == {"output": 3}


async def test_invoke_single_process_in_dict_out_dict(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={
            "one": chain,
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
        },
        input=["input"],
        output=["output"],
    )

    assert app.input_schema.schema() == {
        "title": "PregelInput",
        "type": "object",
        "properties": {"input": {"title": "Input", "type": "integer"}},
    }
    assert app.output_schema.schema() == {
        "title": "PregelOutput",
        "type": "object",
        "properties": {"output": {"title": "Output", "type": "integer"}},
    }
    assert await app.ainvoke({"input": 2}) == {"output": 3}


async def test_invoke_two_processes_in_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("inbox")
    chain_two = Pregel.subscribe_to_each("inbox") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "inbox": channels.Inbox(int),
        },
        input="input",
        output="output",
    )

    assert await app.ainvoke(2) == 4


async def test_invoke_two_processes_in_dict_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("inbox")
    chain_two = Pregel.subscribe_to_each("inbox") | add_one | Pregel.write_to("output")

    pubsub = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "inbox": channels.Inbox(int),
        },
        input=["input", "inbox"],
        output="output",
    )

    # [12 + 1, 2 + 1 + 1]
    assert [c async for c in pubsub.astream({"input": 2, "inbox": 12})] == [13, 4]


async def test_batch_two_processes_in_out() -> None:
    async def add_one_with_delay(inp: int) -> int:
        await asyncio.sleep(inp / 10)
        return inp + 1

    chain_one = (
        Pregel.subscribe_to("input") | add_one_with_delay | Pregel.write_to("one")
    )
    chain_two = (
        Pregel.subscribe_to("one") | add_one_with_delay | Pregel.write_to("output")
    )

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "one": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    assert await app.abatch([3, 2, 1, 3, 5]) == [5, 4, 3, 5, 7]


async def test_invoke_many_processes_in_out(mocker: MockerFixture) -> None:
    test_size = 100
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    chans = {
        "input": channels.LastValue(int),
        "output": channels.LastValue(int),
        "-1": channels.LastValue(int),
    }
    chains = {"-1": Pregel.subscribe_to("input") | add_one | Pregel.write_to("-1")}
    for i in range(test_size - 2):
        chans[str(i)] = channels.LastValue(int)
        chains[str(i)] = (
            Pregel.subscribe_to(str(i - 1)) | add_one | Pregel.write_to(str(i))
        )
    chains["last"] = Pregel.subscribe_to(str(i)) | add_one | Pregel.write_to("output")

    app = Pregel(chains=chains, channels=chans, input="input", output="output")

    # No state is left over from previous invocations
    for _ in range(10):
        assert await app.ainvoke(2, {"recursion_limit": test_size}) == 2 + test_size

    # Concurrent invocations do not interfere with each other
    assert await asyncio.gather(
        *(app.ainvoke(2, {"recursion_limit": test_size}) for _ in range(10))
    ) == [2 + test_size for _ in range(10)]


async def test_batch_many_processes_in_out(mocker: MockerFixture) -> None:
    test_size = 100
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    chans = {
        "input": channels.LastValue(int),
        "output": channels.LastValue(int),
        "-1": channels.LastValue(int),
    }
    chains = {"-1": Pregel.subscribe_to("input") | add_one | Pregel.write_to("-1")}
    for i in range(test_size - 2):
        chans[str(i)] = channels.LastValue(int)
        chains[str(i)] = (
            Pregel.subscribe_to(str(i - 1)) | add_one | Pregel.write_to(str(i))
        )
    chains["last"] = Pregel.subscribe_to(str(i)) | add_one | Pregel.write_to("output")

    app = Pregel(chains=chains, channels=chans, input="input", output="output")

    # No state is left over from previous invocations
    for _ in range(10):
        # Then invoke pubsub
        assert await app.abatch([2, 1, 3, 4, 5], {"recursion_limit": test_size}) == [
            2 + test_size,
            1 + test_size,
            3 + test_size,
            4 + test_size,
            5 + test_size,
        ]

    # Concurrent invocations do not interfere with each other
    assert await asyncio.gather(
        *(
            app.abatch([2, 1, 3, 4, 5], {"recursion_limit": test_size})
            for _ in range(10)
        )
    ) == [
        [2 + test_size, 1 + test_size, 3 + test_size, 4 + test_size, 5 + test_size]
        for _ in range(10)
    ]


async def test_invoke_two_processes_two_in_two_out_invalid(
    mocker: MockerFixture,
) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")
    chain_two = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    with pytest.raises(channels.InvalidUpdateError):
        # LastValue channels can only be updated once per iteration
        await app.ainvoke(2)


async def test_invoke_two_processes_two_in_two_out_valid(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")
    chain_two = Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.Inbox(int),
        },
        input="input",
        output="output",
    )

    # An Inbox channel accumulates updates into a sequence
    assert await app.ainvoke(2) == (3, 3)


async def test_invoke_two_processes_two_in_join_two_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    add_10_each = mocker.Mock(side_effect=lambda x: sorted(y + 10 for y in x))

    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("inbox")
    chain_three = Pregel.subscribe_to("input") | add_one | Pregel.write_to("inbox")
    chain_four = Pregel.subscribe_to("inbox") | add_10_each | Pregel.write_to("output")

    app = Pregel(
        chains={
            "chain_one": chain_one,
            "chain_three": chain_three,
            "chain_four": chain_four,
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "inbox": channels.Inbox(int),
        },
        input="input",
        output="output",
    )

    # Then invoke app
    # We get a single array result as chain_four waits for all publishers to finish
    # before operating on all elements published to topic_two as an array
    for _ in range(100):
        assert await app.ainvoke(2) == [13, 13]

    assert await asyncio.gather(*(app.ainvoke(2) for _ in range(100))) == [
        [13, 13] for _ in range(100)
    ]


async def test_invoke_join_then_call_other_pubsub(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    add_10_each = mocker.Mock(side_effect=lambda x: [y + 10 for y in x])

    inner_app = Pregel(
        chains={
            "one": Pregel.subscribe_to("input") | add_one | Pregel.write_to("output")
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    chain_one = (
        Pregel.subscribe_to("input") | add_10_each | Pregel.write_to("inbox_one").map()
    )
    chain_two = (
        Pregel.subscribe_to("inbox_one")
        | inner_app.map()
        | sorted
        | Pregel.write_to("outbox_one")
    )
    chain_three = Pregel.subscribe_to("outbox_one") | sum | Pregel.write_to("output")

    app = Pregel(
        chains={
            "chain_one": chain_one,
            "chain_two": chain_two,
            "chain_three": chain_three,
        },
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "inbox_one": channels.Inbox(int),
            "outbox_one": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    # Then invoke pubsub
    for _ in range(10):
        assert await app.ainvoke([2, 3]) == 27

    assert await asyncio.gather(*(app.ainvoke([2, 3]) for _ in range(10))) == [
        27 for _ in range(10)
    ]


async def test_invoke_two_processes_one_in_two_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)

    chain_one = (
        Pregel.subscribe_to("input")
        | add_one
        | Pregel.write_to(output=RunnablePassthrough(), between=RunnablePassthrough())
    )
    chain_two = Pregel.subscribe_to("between") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "between": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    # Then invoke pubsub
    assert [c async for c in app.astream(2)] == [3, 4]


async def test_invoke_two_processes_no_out(mocker: MockerFixture) -> None:
    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("between")
    chain_two = Pregel.subscribe_to("between") | add_one

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "between": channels.LastValue(int),
        },
        input="input",
        output="output",
    )

    # Then invoke pubsub
    # It finishes executing (once no more messages being published)
    # but returns nothing, as nothing was published to OUT topic
    assert await app.ainvoke(2) is None


async def test_channel_enter_exit_timing(mocker: MockerFixture) -> None:
    setup_sync = mocker.Mock()
    cleanup_sync = mocker.Mock()
    setup_async = mocker.Mock()
    cleanup_async = mocker.Mock()

    @contextmanager
    def an_int() -> Generator[int, None, None]:
        setup_sync()
        try:
            yield 5
        finally:
            cleanup_sync()

    @asynccontextmanager
    async def an_int_async() -> AsyncGenerator[int, None]:
        setup_async()
        try:
            yield 5
        finally:
            cleanup_async()

    add_one = mocker.Mock(side_effect=lambda x: x + 1)
    chain_one = Pregel.subscribe_to("input") | add_one | Pregel.write_to("inbox")
    chain_two = Pregel.subscribe_to_each("inbox") | add_one | Pregel.write_to("output")

    app = Pregel(
        chains={"chain_one": chain_one, "chain_two": chain_two},
        channels={
            "input": channels.LastValue(int),
            "output": channels.LastValue(int),
            "inbox": channels.Inbox(int),
            "ctx": channels.ContextManager(an_int, an_int_async, typ=int),
        },
        input="input",
        output=["inbox", "output"],
    )

    async def aenumerate(aiter: AsyncIterator[Any]) -> AsyncIterator[tuple[int, Any]]:
        i = 0
        async for chunk in aiter:
            yield i, chunk
            i += 1

    assert setup_sync.call_count == 0
    assert cleanup_sync.call_count == 0
    assert setup_async.call_count == 0
    assert cleanup_async.call_count == 0
    async for i, chunk in aenumerate(app.astream(2)):
        assert setup_sync.call_count == 0, "Sync context manager should not be used"
        assert cleanup_sync.call_count == 0, "Sync context manager should not be used"
        assert setup_async.call_count == 1, "Expected setup to be called once"
        assert cleanup_async.call_count == 0, "Expected cleanup to not be called yet"
        if i == 0:
            assert chunk == {"inbox": (3,)}
        elif i == 1:
            assert chunk == {"output": 4}
        else:
            assert False, "Expected only two chunks"
    assert setup_sync.call_count == 0
    assert cleanup_sync.call_count == 0
    assert setup_async.call_count == 1, "Expected setup to be called once"
    assert cleanup_async.call_count == 1, "Expected cleanup to be called once"
