from __future__ import annotations

import asyncio
import json
import secrets
import socket
import struct
import time
from dataclasses import dataclass


RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")


@dataclass(frozen=True)
class EndpointStatus:
    edition: str
    host: str
    port: int
    online: bool
    latency_ms: float | None = None
    players_online: int | None = None
    players_max: int | None = None
    version: str | None = None
    motd: str | None = None
    error: str | None = None
    error_kind: str | None = None


@dataclass(frozen=True)
class MinecraftStatus:
    java: EndpointStatus
    bedrock: EndpointStatus


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def _read_varint(connection: socket.socket) -> int:
    result = 0
    for position in range(5):
        raw = connection.recv(1)
        if not raw:
            raise ConnectionError("Server đóng kết nối sớm")
        value = raw[0]
        result |= (value & 0x7F) << (7 * position)
        if not value & 0x80:
            return result
    raise ValueError("VarInt quá dài")


def _read_exact(connection: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = connection.recv(length - len(output))
        if not chunk:
            raise ConnectionError("Server đóng kết nối sớm")
        output.extend(chunk)
    return bytes(output)


def _plain_component(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    text = str(value.get("text", ""))
    extra = value.get("extra", [])
    if isinstance(extra, list):
        text += "".join(_plain_component(part) for part in extra)
    return text


def ping_java(host: str, port: int = 25565, timeout: float = 4.0) -> EndpointStatus:
    started = time.perf_counter()
    try:
        encoded_host = host.encode("utf-8")
        handshake = (
            _encode_varint(0)
            + _encode_varint(47)
            + _encode_varint(len(encoded_host))
            + encoded_host
            + struct.pack(">H", port)
            + _encode_varint(1)
        )
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(_encode_varint(len(handshake)) + handshake)
            connection.sendall(b"\x01\x00")
            packet_length = _read_varint(connection)
            if packet_length <= 0 or packet_length > 2 * 1024 * 1024:
                raise ValueError("Gói status không hợp lệ")
            packet_id = _read_varint(connection)
            if packet_id != 0:
                raise ValueError("Sai packet status")
            json_length = _read_varint(connection)
            if json_length <= 0 or json_length > packet_length or json_length > 2 * 1024 * 1024:
                raise ValueError("JSON status không hợp lệ")
            payload = json.loads(_read_exact(connection, json_length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON status không phải object")
        players = payload.get("players", {})
        version = payload.get("version", {})
        return EndpointStatus(
            edition="Java",
            host=host,
            port=port,
            online=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            players_online=int(players.get("online", 0)) if isinstance(players, dict) else None,
            players_max=int(players.get("max", 0)) if isinstance(players, dict) else None,
            version=str(version.get("name", "")) if isinstance(version, dict) else None,
            motd=_plain_component(payload.get("description")),
        )
    except OSError as exc:
        return EndpointStatus(
            "Java", host, port, False,
            error=f"{exc.__class__.__name__}: {exc}", error_kind="network",
        )
    except (ValueError, TypeError, KeyError, struct.error) as exc:
        return EndpointStatus(
            "Java", host, port, False,
            error=f"{exc.__class__.__name__}: {exc}", error_kind="protocol",
        )


def ping_bedrock(host: str, port: int = 19132, timeout: float = 4.0) -> EndpointStatus:
    started = time.perf_counter()
    try:
        timestamp = int(time.time() * 1000)
        client_guid = secrets.randbits(64)
        packet = b"\x01" + struct.pack(">Q", timestamp) + RAKNET_MAGIC + struct.pack(">Q", client_guid)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        if not addresses:
            raise OSError("Không phân giải được hostname")
        family, socktype, protocol, _, address = addresses[0]
        with socket.socket(family, socktype, protocol) as connection:
            connection.settimeout(timeout)
            connection.sendto(packet, address)
            response, _ = connection.recvfrom(65535)
        if len(response) < 35 or response[0] != 0x1C or response[17:33] != RAKNET_MAGIC:
            raise ValueError("RakNet pong không hợp lệ")
        if struct.unpack(">Q", response[1:9])[0] != timestamp:
            raise ValueError("RakNet pong không khớp thời gian ping")
        text_length = struct.unpack(">H", response[33:35])[0]
        if 35 + text_length > len(response):
            raise ValueError("Bedrock status bị thiếu dữ liệu")
        status_text = response[35:35 + text_length].decode("utf-8", errors="replace")
        fields = status_text.split(";")
        if len(fields) < 6 or fields[0] != "MCPE":
            raise ValueError("Bedrock status không hợp lệ")
        return EndpointStatus(
            edition="Bedrock/PE",
            host=host,
            port=port,
            online=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            motd=fields[1],
            version=fields[3],
            players_online=int(fields[4]),
            players_max=int(fields[5]),
        )
    except OSError as exc:
        return EndpointStatus(
            "Bedrock/PE", host, port, False,
            error=f"{exc.__class__.__name__}: {exc}", error_kind="network",
        )
    except (ValueError, TypeError, IndexError, struct.error) as exc:
        return EndpointStatus(
            "Bedrock/PE", host, port, False,
            error=f"{exc.__class__.__name__}: {exc}", error_kind="protocol",
        )


async def query_minecraft_status(
    host: str,
    java_port: int = 25565,
    bedrock_port: int = 19132,
) -> MinecraftStatus:
    java, bedrock = await asyncio.gather(
        asyncio.to_thread(ping_java, host, java_port),
        asyncio.to_thread(ping_bedrock, host, bedrock_port),
    )
    return MinecraftStatus(java=java, bedrock=bedrock)


def concise_status(status: MinecraftStatus) -> str:
    def line(endpoint: EndpointStatus) -> str:
        if not endpoint.online:
            if endpoint.error_kind == "protocol":
                return f"{endpoint.edition}: có phản hồi nhưng không đọc được status"
            return f"{endpoint.edition}: offline/không phản hồi"
        players = (
            f"{endpoint.players_online}/{endpoint.players_max} người"
            if endpoint.players_online is not None and endpoint.players_max is not None
            else "không rõ số người"
        )
        latency = f"{endpoint.latency_ms:.0f} ms" if endpoint.latency_ms is not None else "không rõ ping"
        return f"{endpoint.edition}: online, {players}, {latency}"

    return f"{line(status.java)}; {line(status.bedrock)}"
