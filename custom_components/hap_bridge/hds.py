"""HomeKit Data Stream (HDS): the encrypted TCP channel recordings travel over.

HomeKit Secure Video doesn't use RTP. The home hub writes
SetupDataStreamTransport with a key salt, the accessory answers with a TCP
port and its own salt, and the hub connects. Both sides derive ChaCha20-Poly1305
keys from the HAP session's shared secret, so only the controller that asked
can connect. Messages are an Apple binary encoding of dicts, lists, numbers,
strings and data, each with a header naming the protocol and topic
("control"/"hello", "dataSend"/"open", ...).

Apple doesn't publish this. It follows HAP-NodeJS's reverse-engineered
DataStreamServer and DataStreamParser. Nothing here depends on Home Assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
import os
import struct
from typing import Any
import uuid as uuid_mod

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_LOGGER = logging.getLogger(__name__)

VERSION = "1.0"
MAX_PAYLOAD = 0xFFFFF
PREPARED_TIMEOUT = 10.0  # seconds a prepared session waits for the hub to connect
HELLO_TIMEOUT = 10.0

# Status codes in responses.
STATUS_SUCCESS = 0
STATUS_MISSING_PROTOCOL = 5
STATUS_PROTOCOL_SPECIFIC_ERROR = 6

# Close reasons in the dataSend protocol.
REASON_NORMAL = 0
REASON_NOT_ALLOWED = 1
REASON_BUSY = 2
REASON_CANCELLED = 3
REASON_UNSUPPORTED = 4
REASON_UNEXPECTED_FAILURE = 5
REASON_TIMEOUT = 6
REASON_INVALID_CONFIGURATION = 9

_READ_INFO = b"HDS-Read-Encryption-Key"  # accessory -> controller
_WRITE_INFO = b"HDS-Write-Encryption-Key"  # controller -> accessory


# -- the binary encoding ------------------------------------------------------------


class Int64(int):
    """An integer always sent as 8 bytes (HDS headers use these for ids and status)."""


class _Terminator:
    pass


_TERMINATOR = _Terminator()


def encode(value: Any) -> bytes:
    out = bytearray()
    _encode(value, out)
    return bytes(out)


def _encode(value: Any, out: bytearray) -> None:
    if value is None:
        out.append(0x04)
    elif value is True:
        out.append(0x01)
    elif value is False:
        out.append(0x02)
    elif isinstance(value, Int64):
        out.append(0x33)
        out += struct.pack("<q", int(value))
    elif isinstance(value, int):
        if value == -1:
            out.append(0x07)
        elif 0 <= value <= 39:
            out.append(0x08 + value)
        elif -0x80 <= value <= 0x7F:
            out += b"\x30" + struct.pack("<b", value)
        elif -0x8000 <= value <= 0x7FFF:
            out += b"\x31" + struct.pack("<h", value)
        elif -0x80000000 <= value <= 0x7FFFFFFF:
            out += b"\x32" + struct.pack("<i", value)
        else:
            out += b"\x33" + struct.pack("<q", value)
    elif isinstance(value, float):
        out += b"\x36" + struct.pack("<d", value)
    elif isinstance(value, str):
        _encode_sized(value.encode(), 0x40, 0x61, out)
    elif isinstance(value, bytes | bytearray | memoryview):
        _encode_sized(bytes(value), 0x70, 0x91, out)
    elif isinstance(value, uuid_mod.UUID):
        out += b"\x05" + value.bytes
    elif isinstance(value, list | tuple):
        if len(value) <= 12:
            out.append(0xD0 + len(value))
            for item in value:
                _encode(item, out)
        else:
            out.append(0xDF)
            for item in value:
                _encode(item, out)
            out.append(0x03)
    elif isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if v is not None]
        if len(items) <= 14:
            out.append(0xE0 + len(items))
        else:
            out.append(0xEF)
        for key, item in items:
            _encode(key, out)
            _encode(item, out)
        if len(items) > 14:
            out.append(0x03)
    else:
        raise TypeError(f"HDS can't encode {type(value).__name__}")


def _encode_sized(data: bytes, short_tag: int, long_tag: int, out: bytearray) -> None:
    length = len(data)
    if length <= 32:
        out.append(short_tag + length)
    elif length <= 0xFF:
        out += bytes([long_tag]) + struct.pack("<B", length)
    elif length <= 0xFFFF:
        out += bytes([long_tag + 1]) + struct.pack("<H", length)
    elif length <= 0xFFFFFFFF:
        out += bytes([long_tag + 2]) + struct.pack("<I", length)
    else:
        out += bytes([long_tag + 3]) + struct.pack("<Q", length)
    out += data


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        # Values that later "compressed" tags (0xA0-0xCF) refer back to.
        self.tracked: list[Any] = []

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise ValueError("HDS: unexpected end of data")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def track(self, value: Any) -> Any:
        self.tracked.append(value)
        return value


def decode(data: bytes) -> Any:
    return _decode(_Reader(data))


def _decode(r: _Reader) -> Any:
    tag = r.take(1)[0]
    if tag == 0x01:
        return True
    if tag == 0x02:
        return False
    if tag == 0x03:
        return _TERMINATOR
    if tag == 0x04:
        return None
    if tag == 0x05:
        return r.track(uuid_mod.UUID(bytes=r.take(16)))
    if tag == 0x06:  # seconds since 2001-01-01, as a float
        return r.track(struct.unpack("<d", r.take(8))[0])
    if tag == 0x07:
        return -1
    if 0x08 <= tag <= 0x2E:
        return tag - 0x08
    fixed = {0x30: "<b", 0x31: "<h", 0x32: "<i", 0x33: "<q", 0x35: "<f", 0x36: "<d"}
    if tag in fixed:
        fmt = fixed[tag]
        return r.track(struct.unpack(fmt, r.take(struct.calcsize(fmt)))[0])
    if 0x40 <= tag <= 0x60:
        length = tag - 0x40
        return r.track(r.take(length).decode()) if length else ""
    if 0x61 <= tag <= 0x64:
        return r.track(r.take(_length(r, tag - 0x61)).decode())
    if tag == 0x6F:
        end = r.data.index(0, r.pos)
        value = r.data[r.pos:end].decode()
        r.pos = end + 1
        return r.track(value)
    if 0x70 <= tag <= 0x90:
        length = tag - 0x70
        return r.track(r.take(length)) if length else b""
    if 0x91 <= tag <= 0x94:
        return r.track(r.take(_length(r, tag - 0x91)))
    if 0xA0 <= tag <= 0xCF:
        index = tag - 0xA0
        if index >= len(r.tracked):
            raise ValueError("HDS: back-reference out of range")
        return r.tracked[index]
    if 0xD0 <= tag <= 0xDE:
        return [_decode(r) for _ in range(tag - 0xD0)]
    if tag == 0xDF:
        items = []
        while (item := _decode(r)) is not _TERMINATOR:
            items.append(item)
        return items
    if 0xE0 <= tag <= 0xEE:
        result = {}
        for _ in range(tag - 0xE0):
            key = _decode(r)
            result[key] = _decode(r)
        return result
    if tag == 0xEF:
        result = {}
        while (key := _decode(r)) is not _TERMINATOR:
            result[key] = _decode(r)
        return result
    raise ValueError(f"HDS: unknown tag 0x{tag:02x}")


def _length(r: _Reader, size_index: int) -> int:
    fmt = ("<B", "<H", "<I", "<Q")[size_index]
    return struct.unpack(fmt, r.take(struct.calcsize(fmt)))[0]


# -- frames ----------------------------------------------------------------------------


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack("<Q", counter)


def derive_keys(shared_secret: bytes, controller_salt: bytes, accessory_salt: bytes) -> tuple[bytes, bytes]:
    """(accessory->controller key, controller->accessory key)."""

    def hkdf(info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA512(), length=32, salt=controller_salt + accessory_salt, info=info
        ).derive(shared_secret)

    return hkdf(_READ_INFO), hkdf(_WRITE_INFO)


def seal_frame(key: bytes, counter: int, payload: bytes) -> bytes:
    header = bytes([1]) + len(payload).to_bytes(3, "big")
    return header + ChaCha20Poly1305(key).encrypt(_nonce(counter), payload, header)


def open_frame(key: bytes, counter: int, header: bytes, body: bytes) -> bytes:
    """Decrypt one frame's body (ciphertext + 16-byte tag); raises if it doesn't verify."""
    return ChaCha20Poly1305(key).decrypt(_nonce(counter), body, header)


def pack_message(header: dict[str, Any], message: dict[str, Any]) -> bytes:
    head = encode(header)
    return bytes([len(head)]) + head + encode(message)


def unpack_message(payload: bytes) -> tuple[dict[str, Any], Any]:
    length = payload[0]
    return decode(payload[1:1 + length]), decode(payload[1 + length:])


# -- server ------------------------------------------------------------------------------


@dataclass
class _Prepared:
    read_key: bytes  # accessory -> controller
    write_key: bytes  # controller -> accessory
    expires: float
    client: Any = None


RequestHandler = Callable[["Connection", int, dict[str, Any]], Awaitable[None] | None]
EventHandler = Callable[["Connection", dict[str, Any]], Awaitable[None] | None]


@dataclass
class _Handlers:
    requests: dict[tuple[str, str], RequestHandler] = field(default_factory=dict)
    events: dict[tuple[str, str], EventHandler] = field(default_factory=dict)


class Connection:
    """One hub connected over HDS."""

    def __init__(
        self,
        server: DataStreamServer,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._server = server
        self._reader = reader
        self._writer = writer
        self._read_key: bytes | None = None
        self._write_key: bytes | None = None
        self._out = 0
        self._in = 0
        self.ready = False
        self.closed = False
        self.client: Any = None
        self.on_close: list[Callable[[], None]] = []
        peer = writer.get_extra_info("peername")
        self.remote = f"{peer[0]}:{peer[1]}" if peer else "?"

    # sending

    def send_event(self, protocol: str, topic: str, message: dict[str, Any]) -> None:
        self._send({"protocol": protocol, "event": topic}, message)

    def send_response(
        self, protocol: str, topic: str, request_id: int, status: int = STATUS_SUCCESS,
        message: dict[str, Any] | None = None,
    ) -> None:
        self._send(
            {"protocol": protocol, "response": topic, "id": Int64(request_id),
             "status": Int64(status)},
            message or {},
        )

    def _send(self, header: dict[str, Any], message: dict[str, Any]) -> None:
        if self.closed or self._read_key is None:
            raise ConnectionError("HDS connection is closed")
        payload = pack_message(header, message)
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("HDS payload too large")
        self._writer.write(seal_frame(self._read_key, self._out, payload))
        self._out += 1

    async def drain(self) -> None:
        if not self.closed:
            await self._writer.drain()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._writer.close()
        for callback in self.on_close:
            try:
                callback()
            except Exception:  # noqa: BLE001 - keep closing
                _LOGGER.exception("HDS %s: close handler failed", self.remote)
        self.on_close.clear()

    # receiving

    async def run(self) -> None:
        try:
            await asyncio.wait_for(self._first_frame(), HELLO_TIMEOUT)
            while not self.closed:
                header, body = await self._read_frame()
                if header is None:
                    break
                try:
                    payload = open_frame(self._write_key, self._in, header, body)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001 - tampered or out of order
                    _LOGGER.debug("HDS %s: frame didn't verify", self.remote)
                    break
                self._in += 1
                await self._dispatch(payload)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as err:
            _LOGGER.debug("HDS %s: connection ended: %s", self.remote, err)
        finally:
            self.close()

    async def _read_frame(self) -> tuple[bytes | None, bytes]:
        try:
            header = await self._reader.readexactly(4)
        except asyncio.IncompleteReadError:
            return None, b""
        length = int.from_bytes(header[1:], "big")
        if length > MAX_PAYLOAD:
            raise ValueError("HDS frame too large")
        body = await self._reader.readexactly(length + 16)
        if header[0] != 1:
            return await self._read_frame()  # unknown frame type: skip it
        return header, body

    async def _first_frame(self) -> None:
        """Find which prepared session this is by which keys open the first frame,
        which must be a control/hello request."""
        header, body = await self._read_frame()
        if header is None:
            raise ConnectionError("closed before hello")
        prepared = self._server._claim(header, body)
        if prepared is None:
            raise ConnectionError("no prepared session matches")
        self._read_key, self._write_key = prepared.read_key, prepared.write_key
        self.client = prepared.client
        payload = open_frame(self._write_key, 0, header, body)
        self._in = 1
        head, _message = unpack_message(payload)
        if head.get("protocol") != "control" or head.get("request") != "hello":
            raise ConnectionError("first message wasn't hello")
        self.ready = True
        self.send_response("control", "hello", int(head.get("id", 0)))
        _LOGGER.debug("HDS %s: hello", self.remote)

    async def _dispatch(self, payload: bytes) -> None:
        try:
            head, message = unpack_message(payload)
        except ValueError as err:
            _LOGGER.debug("HDS %s: undecodable message: %s", self.remote, err)
            return
        if not isinstance(head, dict):
            return
        protocol = head.get("protocol")
        message = message if isinstance(message, dict) else {}
        handlers = self._server.handlers
        if (topic := head.get("event")) is not None:
            if (handler := handlers.events.get((protocol, topic))) is not None:
                await _call(handler, self, message)
        elif (topic := head.get("request")) is not None:
            request_id = int(head.get("id", 0))
            if (handler := handlers.requests.get((protocol, topic))) is not None:
                await _call(handler, self, request_id, message)
            else:
                self.send_response(protocol, topic, request_id, STATUS_MISSING_PROTOCOL)
        # Responses: we never send requests, so there's nothing waiting for one.


async def _call(handler: Callable[..., Any], *args: Any) -> None:
    try:
        result = handler(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 - one bad message mustn't drop the connection
        _LOGGER.exception("HDS handler failed")


class DataStreamServer:
    """Listens for hub connections; one per camera."""

    def __init__(self) -> None:
        self.handlers = _Handlers()
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._prepared: list[_Prepared] = []
        self.connections: list[Connection] = []

    def on_request(self, protocol: str, topic: str, handler: RequestHandler) -> None:
        self.handlers.requests[(protocol, topic)] = handler

    def on_event(self, protocol: str, topic: str, handler: EventHandler) -> None:
        self.handlers.events[(protocol, topic)] = handler

    async def async_start(self, host: str | None = None) -> None:
        self._server = await asyncio.start_server(self._accept, host=host, port=0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def async_stop(self) -> None:
        for connection in list(self.connections):
            connection.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def prepare(self, shared_secret: bytes, controller_salt: bytes, client: Any = None) -> bytes:
        """Get ready for a hub to connect; returns the accessory's key salt."""
        accessory_salt = os.urandom(32)
        read_key, write_key = derive_keys(shared_secret, controller_salt, accessory_salt)
        now = asyncio.get_running_loop().time()
        self._prepared = [p for p in self._prepared if p.expires > now]
        self._prepared.append(_Prepared(read_key, write_key, now + PREPARED_TIMEOUT, client))
        return accessory_salt

    def _claim(self, header: bytes, body: bytes) -> _Prepared | None:
        now = asyncio.get_running_loop().time()
        for prepared in self._prepared:
            if prepared.expires < now:
                continue
            try:
                open_frame(prepared.write_key, 0, header, body)
            except Exception:  # noqa: BLE001 - not this one
                continue
            self._prepared.remove(prepared)
            return prepared
        return None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = Connection(self, reader, writer)
        self.connections.append(connection)
        connection.on_close.append(lambda: self.connections.remove(connection))
        await connection.run()
