import asyncio
started = []

async def StartAsyncTcpServer(context, identity=None, address=None):
    started.append(address)
    await asyncio.Event().wait()
