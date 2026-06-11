import asyncio
from datetime import date
from typing import Any, Awaitable, Callable

from loguru import logger

from .parser import ParsedInput, parse_with_retry


class ParseQueue:
    """串行限速队列：所有 LLM 调用（文字解析、收据视觉解析）共用同一 RPM 预算。"""

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

    async def submit_job(
        self,
        factory: Callable[[], Awaitable[Any]],
        label: str = "job",
    ) -> Any:
        """把任意 async 任务排入限速队列，串行执行后返回其结果。"""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put((fut, factory, label))
        return await fut

    async def submit(
        self,
        user_input: str,
        today: date | None = None,
        existing_items: list[str] | None = None,
    ) -> ParsedInput:
        async def job() -> ParsedInput:
            result = await parse_with_retry(user_input, today, existing_items)
            ops = [f"{o.intent} {o.item}" for o in result.operations]
            logger.info(f"← llm: kind={result.kind} conf={result.confidence:.2f} ops={ops}")
            logger.info(f"  reasoning: {result.reasoning}")
            return result

        return await self.submit_job(job, label=f"llm: {user_input!r}")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            fut, factory, label = await self._queue.get()
            try:
                if self._min_interval > 0:
                    elapsed = loop.time() - self._last_call
                    if elapsed < self._min_interval:
                        await asyncio.sleep(self._min_interval - elapsed)
                self._last_call = loop.time()
                logger.info(f"→ {label}")
                result = await factory()
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                logger.exception("queue job failed")
                if not fut.done():
                    fut.set_exception(e)
            finally:
                self._queue.task_done()
