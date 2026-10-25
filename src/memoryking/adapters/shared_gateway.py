"""Bounded TLS-byte relay for the local Compose ingress; no secrets or HTTP parsing."""

from __future__ import annotations

import asyncio


class Gateway:
    def __init__(self, host: str = "api", port: int = 8765, limit: int = 32):
        self.host, self.port, self.limit = host, port, limit
        self.active = 0

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.limit:
            writer.close()
            await writer.wait_closed()
            return
        self.active += 1
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(60):
                source, target = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, limit=65536), timeout=5
                )
                upstream = target
                tasks = [
                    asyncio.create_task(self.pipe(reader, target)),
                    asyncio.create_task(self.pipe(source, writer)),
                ]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, TimeoutError):
            pass
        finally:
            self.active -= 1
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass

    @staticmethod
    async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await asyncio.wait_for(reader.read(65536), timeout=30):
            writer.write(data)
            await writer.drain()


async def main() -> None:
    gateway = Gateway()
    server = await asyncio.start_server(gateway.accept, "0.0.0.0", 8765, backlog=32)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
