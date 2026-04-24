import asyncio
from datetime import date

from loguru import logger

from llm import ParsedInput, parse_with_retry


class ParseQueue:
    def __init__(self, min_interval: float = 0.0):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._min_interval = min_interval
        self._last_call: float = 0.0

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is None:
            return
        await self._queue.join()
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None

    async def submit(
        self,
        user_input: str,
        today: date | None = None,
        existing_items: list[str] | None = None,
    ) -> ParsedInput:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ParsedInput] = loop.create_future()
        await self._queue.put((fut, user_input, today, existing_items))
        return await fut

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            fut, user_input, today, existing_items = await self._queue.get()
            try:
                if self._min_interval > 0:
                    elapsed = loop.time() - self._last_call
                    if elapsed < self._min_interval:
                        await asyncio.sleep(self._min_interval - elapsed)
                self._last_call = loop.time()
                logger.info(f"→ gemini: {user_input!r}")
                result = await parse_with_retry(user_input, today, existing_items)
                ops = [f"{o.intent} {o.item}" for o in result.operations]
                logger.info(f"← gemini: kind={result.kind} conf={result.confidence:.2f} ops={ops}")
                logger.info(f"  reasoning: {result.reasoning}")
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                logger.exception("parse failed")
                if not fut.done():
                    fut.set_exception(e)
            finally:
                self._queue.task_done()
