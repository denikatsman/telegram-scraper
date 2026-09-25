# Lifecycle and cleanup audit

Verified on 2026-09-19 against the current working tree, including its pre-existing uncommitted changes. HEAD was `188afe9`. The reviewed Git blob hashes were `06f3e5f478006f05d90d5ad43f837075d0a78bd1` for `telegram_scraper/engine.py` and `2be026e052e642918f2c794d126778969e784f89` for `telegram_scraper/server.py`.

Two concrete findings were reproduced with Python 3.14.5 and the installed Telethon 1.42.0. The probes used memory sessions, temporary libraries, and either no network or a loopback-only TCP fixture. No real Telegram account, user archive, running app, or production code was changed.

## LC-01 — [P2] Watch accumulates disconnection waiters until the account connection closes

**Location:** [engine.py:963](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:963), particularly lines 966–967 and 980–985.

Each `_wait_for_update()` call obtains `client.disconnected` and shields it again:

```python
disconnected = getattr(client, "disconnected", None)
shield = asyncio.ensure_future(asyncio.shield(disconnected)) if disconnected is not None else None
```

In the installed Telethon implementation, `TelegramBaseClient.disconnected` delegates to `MTProtoSender.disconnected`, which already returns a newly allocated `asyncio.shield(self._disconnected)`. The app therefore creates two layers of wrapper futures. Its `finally` cancels only its own outer `shield`; the Telethon-created inner wrapper is never cancelled or otherwise disposed. That wrapper remains attached to the sender's long-lived disconnection future.

**Trigger and impact:** Leave Watch connected through repeated timer or message wakeups. Every completed wait leaves another wrapper and callbacks attached to the connection. Stopping Watch removes its event handlers but does not disconnect the shared client, so these references survive Stop and subsequent Watch runs. Memory retained by the waiters grows with the number of waits until the account connection actually ends. On the tested Python version, the callbacks also retain the task that created the shield.

**Verified result:** A probe called the real `_wait_for_update()` with a real Telethon client's disconnected property and a controlled, unresolved connection future. Each wake event was already set, so every wait completed normally. After yielding to cleanup callbacks and forcing garbage collection:

| Completed waits | Callbacks still attached to connection future | Finished probe wait tasks still retained |
| ---: | ---: | ---: |
| 0 | 0 | 0 |
| 1 | 2 | 1 |
| 10 | 20 | 10 |
| 100 | 200 | 100 |

Resolving the underlying connection future released all 200 callbacks and all 100 finished probe tasks. Each probe wait used a separate task to make retention measurable; normal Watch awaits its waits within the same job task. The unbounded wrapper/callback accumulation is present in either case.

**Dependency evidence:** [TelegramBaseClient.disconnected, lines 463–477](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/client/telegrambaseclient.py:463) and [MTProtoSender.disconnected, lines 208–217](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:208).

**Correction needed:** Give the returned disconnection waiter an explicit lifetime. Avoid creating an unowned intermediate shield on every wait, and dispose of the wrapper without cancelling the sender's actual disconnection future. Add a regression that performs many wakeups and stops/restarts Watch while leaving the shared account connected; assert that retained waiters/callbacks remain bounded.

Reproduction, run from the repository root:

```sh
/usr/local/opt/python@3.14/bin/python3.14 -B - <<'PY'
import asyncio, gc, time, weakref
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telegram_scraper.engine import TelegramService

async def main():
    client = TelegramClient(MemorySession(), 123, 'a' * 32)
    connection_future = asyncio.get_running_loop().create_future()
    # Fixture only: emulate an open connection without connecting to Telegram.
    client._sender._MTProtoSender__disconnected = connection_future
    service = TelegramService(Path('/tmp'), {}, None, lambda update: None)
    service._running = True
    references = []
    for count in range(101):
        if count in (0, 1, 10, 100):
            gc.collect()
            print(count, len(connection_future._callbacks or []),
                  sum(reference() is not None for reference in references))
        if count == 100:
            break
        service._next_catch_up = time.monotonic() + 60
        wake = asyncio.Event()
        wake.set()
        task = asyncio.create_task(service._wait_for_update(client, wake))
        references.append(weakref.ref(task))
        await task
        del task
        await asyncio.sleep(0)
    connection_future.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    print('after disconnect', len(connection_future._callbacks or []),
          sum(reference() is not None for reference in references))
    client.session.close()

asyncio.run(main())
PY
```

## LC-02 — [P2] A timed-out initial login connection leaks its transport on the next attempt

**Location:** [engine.py:405](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:405), lines 405–406. The reachable cancellation path is [server.py:87](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/server.py:87), lines 87–93, through `Runtime.authenticate()` at lines 500–522. Later cleanup is [engine.py:1099](/Users/xxx/GitHub/telegram-scraper/telegram_scraper/engine.py:1099).

`_connected_client()` awaits `self._client.connect()` without cleaning up a partially established connection when that await is cancelled. `Runtime.call()` cancels the authentication coroutine on its 45-second timeout, while `authenticate()` clears `auth_busy` and leaves the same service/client available for another attempt.

During a first login, Telethon can have an open TCP transport and its two transport tasks while it is still waiting for the initial authorization-key handshake. At that point `client.is_connected()` is still false: Telethon sets its sender's `_user_connected` only after `_connect()` finishes. Cancelling that handshake does not disconnect its transport. The next call to `connect()` creates a new transport and replaces the sender's reference to the previous one. The previous transport's send/receive tasks keep it alive, but the app has lost its cleanup path to it.

**Trigger and impact:** A new session's TCP connection succeeds, but the peer stalls during the initial handshake long enough for the app's authentication timeout. Retrying repeats the leak. Each such retry leaves an open socket and two live transport tasks. Closing the service or replacing its credentials subsequently disconnects only the newest transport; earlier transports remain alive in the worker loop until they fail independently or the process exits.

**Verified result:** The real `Runtime.call(Runtime.authenticate("code", ...))` path was exercised against a local TCP server that accepted and read the handshake bytes but never replied. The production timeout was shortened to 150 ms through the existing `Runtime.call(timeout=...)` parameter. No Telethon handshake or disconnect implementation was mocked. A transport subclass asserted that its destination was `127.0.0.1` and recorded the constructed connections.

| Point in execution | `auth_busy` | Open fixture connections | Live transport tasks |
| --- | --- | ---: | ---: |
| After first timeout | false | 1 | 2 |
| After second timeout | false | 2 | 4 |
| After third timeout | false | 3 | 6 |
| After `await runtime.service.close()` | false | 2 | 4 |

The probe explicitly disconnected the otherwise orphaned transports before closing its temporary runtime. A separate direct-cancellation probe of `_connected_client()` produced the same counts.

**Dependency evidence:** [TelegramBaseClient.connect, lines 492–611](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/client/telegrambaseclient.py:492), [MTProtoSender.connect, lines 123–135](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:123), [Connection.connect, lines 246–255](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/connection/connection.py:246), and [MTProtoSender._disconnect, lines 320–352](/Users/xxx/Library/Python/3.14/lib/python/site-packages/telethon/network/mtprotosender.py:320). Together they establish the intermediate transport lifetime, the reference replacement on retry, and why ordinary close only reaches the newest transport.

**Correction needed:** If connection establishment is cancelled or fails after transport creation, drain disconnect cleanup before permitting the client to reconnect or replacing it. Preserve cancellation propagation while ensuring cleanup itself finishes. Add a regression that times out during the initial handshake, retries, and then closes the service; every transport and transport task should be gone.

Reproduction, run from the repository root:

```sh
/usr/local/opt/python@3.14/bin/python3.14 -B -u - <<'PY'
import asyncio, json, tempfile
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.network.connection.tcpfull import ConnectionTcpFull
from telegram_scraper.server import Runtime

class LocalConnection(ConnectionTcpFull):
    made = []
    async def connect(self, *args, **kwargs):
        assert self._ip == '127.0.0.1'
        await super().connect(*args, **kwargs)
        self.made.append(self)

with tempfile.TemporaryDirectory(prefix='lifecycle-runtime-timeout-') as directory:
    runtime = Runtime(Path(directory))
    accepted, handlers = set(), set()

    async def hold(reader, writer):
        handlers.add(asyncio.current_task())
        accepted.add(writer)
        try:
            while await reader.read(4096):
                pass
        finally:
            accepted.discard(writer)
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    async def setup():
        server = await asyncio.start_server(hold, '127.0.0.1', 0)
        session = MemorySession()
        session.set_dc(2, '127.0.0.1', server.sockets[0].getsockname()[1])
        client = TelegramClient(session, 123, 'a' * 32,
            connection=LocalConnection, connection_retries=1, timeout=1,
            catch_up=False, sequential_updates=True)
        runtime.get_service()._client = client
        return server

    server = runtime.call(setup())

    async def snapshot():
        while runtime.auth_busy:
            await asyncio.sleep(.001)
        await asyncio.sleep(.01)
        return {'auth_busy': runtime.auth_busy,
                'connections_open': len(accepted),
                'transport_tasks': sum(not task.done()
                    for connection in LocalConnection.made
                    for task in (connection._send_task, connection._recv_task))}

    async def close_service():
        await runtime.service.close()
        return await snapshot()

    async def cleanup():
        for connection in LocalConnection.made:
            await connection.disconnect()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*tuple(handlers))

    try:
        for number in range(1, 4):
            try:
                runtime.call(runtime.authenticate('code',
                    {'phone': '+41000000001'}), timeout=.15)
            except ValueError as error:
                assert 'taking too long' in str(error)
            print(json.dumps({'after_timeout': number,
                              **runtime.call(snapshot())}))
        print(json.dumps({'after_service_close': runtime.call(close_service())}))
    finally:
        runtime.call(cleanup())
        runtime.close()
PY
```

## Validation boundary

Both findings are resource-lifetime failures demonstrated against the installed Telethon implementation. The probes do not establish a live Telegram outage or estimate production memory consumption. Findings about lost updates, retry semantics, persistence, and general error reporting are outside this report.

Thirteen existing tests covering desktop startup/reuse/shutdown, download cancellation, Watch handler removal/catch-up/draining, disk-worker draining, and cancelled channel selection were also run. Eleven passed in the first run. The download-cancellation and Watch-overflow tests hit their existing 2-second/5-second fixture deadlines in that run, then both passed in an isolated repeat after pre-importing Telethon (`2 passed in 1.46s`). Those tests do not cover either leak reproduced above.
