import asyncio
import logging

import nest_asyncio

logger = logging.getLogger(__name__)


def apply():
    nest_asyncio.apply()

    # shorthand for the class whose __del__ raises the exception
    _BEL = asyncio.base_events.BaseEventLoop
    _UWP = getattr(asyncio.unix_events, "_UnixWritePipeTransport", None)
    _TAS = asyncio.tasks.Task

    _original_del = _BEL.__del__

    def _patched_del(self):
        try:
            # invoke the original method...
            _original_del(self)
        except Exception as e:  # type: ignore
            # ... but ignore any exceptions it might raise
            # NOTE: horrible anti-pattern. Just using because
            # jupyter notebooks don't play nice with asyncio
            logger.debug(f"Exception in __del__ of {self}: {e}")
            pass

    # replace the original __del__ method
    for op in (_BEL, _TAS, _UWP):
        if op is not None:
            op.__del__ = _patched_del
