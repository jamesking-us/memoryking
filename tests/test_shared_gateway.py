import asyncio
import unittest

from memoryking.adapters.shared_gateway import Gateway


class SharedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(16384):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.gateway = Gateway("127.0.0.1", self.upstream.sockets[0].getsockname()[1], limit=1)
        self.server = await asyncio.start_server(self.gateway.accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for server in (self.server, self.upstream):
            server.close()
            await server.wait_closed()

    async def settled(self):
        async with asyncio.timeout(2):
            while self.gateway.active:
                await asyncio.sleep(0.01)

    async def test_preserves_opaque_bytes_larger_than_relay_buffer(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        payload = bytes(range(256)) * 1024
        writer.write(payload)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(payload)), 2), payload)
        writer.close()
        await writer.wait_closed()
        await self.settled()

    async def test_capacity_rejects_then_recovers_after_disconnect(self):
        first, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"first")
        await writer.drain()
        self.assertEqual(await first.readexactly(5), b"first")
        denied, extra = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(denied.read(1), 2), b"")
        self.assertEqual(self.gateway.active, 1)
        extra.close()
        await extra.wait_closed()
        writer.close()
        await writer.wait_closed()
        await self.settled()
        again, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"again")
        await writer.drain()
        self.assertEqual(await again.readexactly(5), b"again")
        writer.close()
        await writer.wait_closed()
        await self.settled()

    async def test_unavailable_fixed_upstream_closes_and_releases_capacity(self):
        self.upstream.close()
        await self.upstream.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.assertEqual(await asyncio.wait_for(reader.read(1), 2), b"")
        writer.close()
        await writer.wait_closed()
        await self.settled()
