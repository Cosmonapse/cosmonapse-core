"""
cosmonapse.synapse.dev
~~~~~~~~~~~~~~~~~~~~~~~~
Local dev Synapse  -  a tiny TCP + NDJSON broker.

`cosmo synapse start memory` boots a `DevSynapseServer` and prints a URL like
`cosmo://127.0.0.1:7070`. Any process can then connect to that URL with
`synapse = await connect_synapse('cosmo://...')` and hand the result to a
`Dendrite(synapse=synapse, namespace=...)` to start exchanging Signals.

This is **not** a production synapse. It is the equivalent of
`MemorySynapse` for the case where Axons, Dendrites and Cortices
live in separate processes on a developer's laptop. For production
use NatsSynapse or KafkaSynapse.

Wire protocol (NDJSON over TCP, UTF-8, `\n` framed)
---------------------------------------------------
Client -> Server:
    {"op":"hello"}
    {"op":"pub","subject":"a.b.c","frame":"<utf8 JSON of Signal>"}
    {"op":"sub","sub_id":"s1","subject":"a.b.*","queue_group":null}
    {"op":"unsub","sub_id":"s1"}

    Namespace management:
    {"op":"ns_register","namespace":"dev","transport":"memory"}
    {"op":"mgmt_list"}
    {"op":"mgmt_info","namespace":"dev"}
    {"op":"mgmt_stop","namespace":"dev"}

Server -> Client:
    {"op":"welcome"}
    {"op":"msg","sub_id":"s1","subject":"a.b.c","frame":"<utf8 JSON>"}
    {"op":"err","message":"..."}

    Namespace management:
    {"op":"ns_registered","namespace":"dev"}
    {"op":"mgmt_ns_list","namespaces":[...]}
    {"op":"mgmt_ns_info","namespace":"dev",...}
    {"op":"mgmt_stop_ack","namespace":"dev"}
    {"op":"ns_stopping","namespace":"dev"}   <- sent to owner on mgmt_stop

Subject matching uses the same `*` (one token) / `>` (rest) wildcards
as MemorySynapse and NATS. Queue groups load-balance round-robin
within the group. Subscribers with no queue_group (Dopplers) each
receive every matching message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from cosmonapse.envelope import Signal
from cosmonapse.synapse.base import MessageHandler, Subscription, Synapse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subject matching (shared with MemorySynapse semantics)
# ---------------------------------------------------------------------------

def _matches(pattern: str, subject: str) -> bool:
    if pattern == subject:
        return True
    p = pattern.split(".")
    s = subject.split(".")
    i = j = 0
    while i < len(p) and j < len(s):
        if p[i] == ">":
            return True
        if p[i] == "*":
            i += 1
            j += 1
            continue
        if p[i] != s[j]:
            return False
        i += 1
        j += 1
    return i == len(p) and j == len(s)


# ---------------------------------------------------------------------------
# Server-side
# ---------------------------------------------------------------------------

class _ClientSession:
    """One connected TCP client. Owns its writer + its subscriptions."""

    def __init__(self, server: "DevSynapseServer", writer: asyncio.StreamWriter,
                 peer: str) -> None:
        self.server = server
        self.writer = writer
        self.peer = peer
        self.subs: dict[str, tuple[str, str | None]] = {}   # sub_id -> (subject, queue_group)
        self._send_lock = asyncio.Lock()
        self._alive = True

    async def send(self, payload: dict[str, Any]) -> None:
        if not self._alive:
            return
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._send_lock:
            try:
                self.writer.write(line)
                await self.writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                self._alive = False

    def close(self) -> None:
        self._alive = False
        try:
            self.writer.close()
        except Exception:
            pass


class DevSynapseServer:
    """
    Asyncio TCP server speaking the dev-synapse wire protocol.

    Usage:
        server = DevSynapseServer(host="127.0.0.1", port=7070)
        await server.start()
        print(server.url)              # cosmo://127.0.0.1:7070
        ...
        await server.stop()

    Namespace management
    --------------------
    Connected clients can register namespaces with ns_register, and
    external tools (cosmo synapse view / stop) can query or stop them
    via mgmt_list / mgmt_info / mgmt_stop.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7070,
    ) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.base_events.Server | None = None
        self._sessions: set[_ClientSession] = set()
        self._rr_counters: dict[str, int] = defaultdict(int)
        # Optional observer hook: every published Signal is passed here
        # before fan-out as (subject, frame). Used by `cosmo synapse start`
        # to stream to stdout.
        self.on_signal: Callable[[str, str], None] | None = None
        # Namespace registry: namespace -> {transport, started_at, signal_count, owner}
        self._namespaces: dict[str, Any][str, dict[str, Any]] = {}

    # -- properties ---------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"cosmo://{self._host}:{self._port}"

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    # -- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        # If port was 0, capture the OS-assigned one.
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        logger.info("DevSynapseServer listening on %s", self.url)

    async def stop(self) -> None:
        if self._server is None:
            return
        # Close all sessions.
        for s in list(self._sessions):
            s.close()
        self._sessions.clear()
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("DevSynapseServer stopped")

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    # -- client handling ----------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_str = f"{peer[0]}:{peer[1]}" if peer else "?"
        session = _ClientSession(self, writer, peer_str)
        self._sessions.add(session)
        logger.debug("DevSynapseServer: client %s connected", peer_str)
        try:
            await session.send({"op": "welcome"})
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    await session.send({"op": "err",
                                        "message": f"bad JSON: {exc}"})
                    continue
                await self._handle_op(session, msg)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as exc:
            logger.exception("DevSynapseServer: client %s crashed: %s",
                             peer_str, exc)
        finally:
            self._sessions.discard(session)
            session.close()
            # Clean up any namespaces this session owned.
            for ns in [k for k, v in self._namespaces.items()
                       if v.get("owner") is session]:
                del self._namespaces[ns]
                logger.debug("DevSynapseServer: namespace %r removed (owner disconnected)", ns)
            logger.debug("DevSynapseServer: client %s disconnected", peer_str)

    async def _handle_op(self, session: _ClientSession, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "pub":
            subject = msg.get("subject")
            frame = msg.get("frame")
            if not subject or frame is None:
                await session.send({"op": "err", "message": "pub: missing subject/frame"})
                return
            await self._deliver(subject, frame)
        elif op == "sub":
            sub_id = msg.get("sub_id")
            subject = msg.get("subject")
            queue_group = msg.get("queue_group")
            if not sub_id or not subject:
                await session.send({"op": "err", "message": "sub: missing sub_id/subject"})
                return
            session.subs[sub_id] = (subject, queue_group)
        elif op == "unsub":
            sub_id = msg.get("sub_id")
            if sub_id in session.subs:
                del session.subs[sub_id]
        elif op == "hello" or op == "ping":
            await session.send({"op": "welcome" if op == "hello" else "pong"})
        # ------------------------------------------------------------------
        # Namespace management ops
        # ------------------------------------------------------------------
        elif op == "ns_register":
            namespace = msg.get("namespace")
            transport = msg.get("transport", "memory")
            if not namespace:
                await session.send({"op": "err", "message": "ns_register: missing namespace"})
                return
            self._namespaces[namespace] = {
                "transport": transport,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "signal_count": 0,
                "owner": session,
            }
            logger.debug("DevSynapseServer: namespace %r registered (transport=%s)",
                         namespace, transport)
            await session.send({"op": "ns_registered", "namespace": namespace})
        elif op == "mgmt_list":
            ns_list = [
                {
                    "namespace": ns,
                    "transport": info["transport"],
                    "started_at": info["started_at"],
                    "signal_count": info["signal_count"],
                }
                for ns, info in self._namespaces.items()
            ]
            await session.send({"op": "mgmt_ns_list", "namespaces": ns_list})
        elif op == "mgmt_info":
            namespace = msg.get("namespace")
            if namespace not in self._namespaces:
                await session.send({"op": "err",
                                    "message": f"namespace {namespace!r} not found"})
                return
            info = self._namespaces[namespace]
            await session.send({
                "op": "mgmt_ns_info",
                "namespace": namespace,
                "transport": info["transport"],
                "started_at": info["started_at"],
                "signal_count": info["signal_count"],
                "client_count": len(self._sessions),
            })
        elif op == "mgmt_stop":
            namespace = msg.get("namespace")
            if namespace not in self._namespaces:
                await session.send({"op": "err",
                                    "message": f"namespace {namespace!r} not found"})
                return
            info = self._namespaces.pop(namespace)
            owner: "_ClientSession | None" = info.get("owner")
            if owner and owner._alive:
                await owner.send({"op": "ns_stopping", "namespace": namespace})
            await session.send({"op": "mgmt_stop_ack", "namespace": namespace})
            logger.debug("DevSynapseServer: namespace %r stopped via mgmt_stop", namespace)
        else:
            await session.send({"op": "err", "message": f"unknown op {op!r}"})

    async def _deliver(self, subject: str, frame: str) -> None:
        # Track signal count for the namespace this subject belongs to.
        # Subjects follow the convention: cosmonapse.<namespace>.<TYPE>[...]
        parts = subject.split(".")
        if len(parts) >= 2 and parts[0] == "cosmonapse":
            ns = parts[1]
            if ns in self._namespaces:
                self._namespaces[ns]["signal_count"] += 1

        # Notify observer (e.g. cosmo synapse start stdout doppler)
        if self.on_signal is not None:
            try:
                self.on_signal(subject, frame)
            except Exception as exc:
                logger.warning("DevSynapseServer.on_signal raised: %s", exc)

        solo: list[tuple[_ClientSession, str]] = []
        groups: dict[str, list[tuple[_ClientSession, str]]] = defaultdict(list)

        for session in list(self._sessions):
            for sub_id, (pat, group) in session.subs.items():
                if not _matches(pat, subject):
                    continue
                if group is None:
                    solo.append((session, sub_id))
                else:
                    groups[group].append((session, sub_id))

        msg = {"op": "msg", "subject": subject, "frame": frame}

        # Fan out to all solo subscribers
        for session, sub_id in solo:
            await session.send({**msg, "sub_id": sub_id})

        # Round-robin within each queue group
        for group, members in groups.items():
            idx = self._rr_counters[group] % len(members)
            self._rr_counters[group] += 1
            session, sub_id = members[idx]
            await session.send({**msg, "sub_id": sub_id})


# ---------------------------------------------------------------------------
# Client-side: DevSynapse
# ---------------------------------------------------------------------------

class _DevSubscription(Subscription):
    def __init__(self, synapse: "DevSynapse", sub_id: str) -> None:
        self._synapse = synapse
        self._sub_id = sub_id
        self._active = True

    async def unsubscribe(self) -> None:
        if not self._active:
            return
        self._active = False
        await self._synapse._send({"op": "unsub", "sub_id": self._sub_id})
        self._synapse._handlers.pop(self._sub_id, None)


class DevSynapse(Synapse):
    """
    TCP / NDJSON client speaking to a DevSynapseServer.

    Parameters
    ----------
    host  Server host. Default '127.0.0.1'.
    port  Server port. Default 7070.
    url   Convenience: pass cosmo://host:port instead of host/port.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        url: str | None = None,
    ) -> None:
        if url is not None:
            from urllib.parse import urlparse
            p = urlparse(url)
            if p.scheme != "cosmo":
                raise ValueError(f"DevSynapse expects scheme cosmo://, got {p.scheme!r}")
            host = host or p.hostname or "127.0.0.1"
            # Preserve an explicitly-requested port of 0 (OS-assigned); only
            # fall back when no port was given at all.
            if port is None:
                port = p.port if p.port is not None else 7070
        self._host = host or "127.0.0.1"
        self._port = port if port is not None else 7070

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._send_lock = asyncio.Lock()
        self._handlers: dict[str, Any][str, MessageHandler] = {}
        self._connected = False

    @property
    def url(self) -> str:
        return f"cosmo://{self._host}:{self._port}"

    # -- lifecycle ----------------------------------------------------

    async def connect(self) -> None:
        if self._connected:
            return
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port,
        )
        # Read the welcome banner.
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("DevSynapse: server closed before welcome")
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("DevSynapse connected to %s", self.url)

    async def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._handlers.clear()

    # -- protocol -----------------------------------------------------

    async def _send(self, payload: dict) -> None:
        if not self._connected or self._writer is None:
            raise RuntimeError("DevSynapse not connected")
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        async with self._send_lock:
            self._writer.write(line)
            await self._writer.drain()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    logger.warning("DevSynapse: server closed connection")
                    self._connected = False
                    return
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    logger.warning("DevSynapse: bad frame: %s", exc)
                    continue
                op = msg.get("op")
                if op == "msg":
                    sub_id = msg.get("sub_id")
                    handler = self._handlers.get(sub_id)
                    if handler is None:
                        continue
                    try:
                        signal = Signal.decode(msg["frame"])
                    except Exception as exc:
                        logger.warning("DevSynapse: decode failed: %s", exc)
                        continue
                    try:
                        result = handler(signal)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.exception("DevSynapse: handler raised: %s", exc)
                elif op == "err":
                    logger.warning("DevSynapse: server error: %s",
                                   msg.get("message"))
        except asyncio.CancelledError:
            return

    # -- Synapse surface --------------------------------------------

    async def publish(self, subject: str, signal: Signal) -> None:
        frame = signal.encode().decode("utf-8")
        await self._send({"op": "pub", "subject": subject, "frame": frame})

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        queue_group: str | None = None,
    ) -> Subscription:
        sub_id = uuid.uuid4().hex
        self._handlers[sub_id] = handler
        await self._send({
            "op": "sub",
            "sub_id": sub_id,
            "subject": subject,
            "queue_group": queue_group,
        })
        return _DevSubscription(self, sub_id)

    async def request(
        self,
        subject: str,
        signal: Signal,
        *,
        timeout_s: float = 5.0,
    ) -> Signal:
        reply_subject = f"_inbox.{uuid.uuid4().hex}"
        fut: asyncio.Future[Signal] = asyncio.get_running_loop().create_future()

        async def _on_reply(reply: Signal) -> None:
            if not fut.done():
                fut.set_result(reply)

        sub = await self.subscribe(reply_subject, _on_reply)
        try:
            enriched = signal.model_copy(
                update={"meta": {**signal.meta, "_reply_to": reply_subject}}
            )
            await self.publish(subject, enriched)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"DevSynapse: no reply on {reply_subject!r} within {timeout_s}s"
            )
        finally:
            await sub.unsubscribe()
