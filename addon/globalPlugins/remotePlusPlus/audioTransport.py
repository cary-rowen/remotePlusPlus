"""The server's version-1 TCP/UDP protocol, independent of NVDA and audio devices."""

import json
import select
import socket
import struct
import time
from threading import BoundedSemaphore, Event, Thread
from concurrent.futures import Future
from typing import Any, cast

HEADER = struct.Struct(">4sBB16sQQ")
UINT64_MASK = (1 << 64) - 1

# ponytail: one OS resolver at a time; native cancellable DNS if a stuck
# resolver must be replaceable before Windows finishes its own retries.
_dnsSlot = BoundedSemaphore(1)


def resolve(host: str, port: int, stop: Event, deadline: float) -> list[Any]:
	while not stop.is_set():
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise TimeoutError("Audio DNS lookup timed out")
		if _dnsSlot.acquire(timeout=min(0.05, remaining)):
			break
	else:
		raise OSError("Audio start cancelled")
	result: Future[list[Any]] = Future()

	def lookup() -> None:
		# Capture only DNS inputs/result, never the audio runtime or device objects.
		try:
			result.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
		except Exception as error:
			result.set_exception(OSError(str(error)))
		finally:
			_dnsSlot.release()

	try:
		Thread(target=lookup, name="remotePlusPlusAudioDNS", daemon=True).start()
	except RuntimeError:
		_dnsSlot.release()
		raise
	while not stop.is_set():
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise TimeoutError("Audio DNS lookup timed out")
		try:
			return result.result(timeout=min(0.05, remaining))
		except TimeoutError:
			continue
	raise OSError("Audio start cancelled")


class AudioError(Exception):
	def __init__(self, code: str, message: str) -> None:
		super().__init__(message)
		self.code = code


def audioPacket(session: bytes, sequence: int, payload: bytes) -> bytes:
	if len(session) != 16 or not 0 < len(payload) <= 1200:
		raise ValueError("Invalid audio packet size")
	return HEADER.pack(b"RAS1", 1, 4, session, sequence, time.time_ns() // 1_000_000) + payload


def parseAudio(packet: bytes, session: bytes, frameBytes: int) -> tuple[int, bytes] | None:
	if len(packet) != HEADER.size + frameBytes:
		return None
	magic, version, kind, identity, sequence, _ = HEADER.unpack_from(packet)
	if (magic, version, kind, identity) != (b"RAS1", 1, 4, session):
		return None
	return sequence, packet[HEADER.size :]


class Session:
	def __init__(self, stop: Event) -> None:
		self.stop = stop
		self.tcp: socket.socket | None = None
		self.udp: socket.socket | None = None
		self.identity = b""
		self.nextTcp = self.nextUdp = 0.0
		self.tcpInterval = self.udpInterval = 5.0
		self.controlBuffer = bytearray()

	def open(self, host: str, port: int, key: str, role: str, frameBytes: int) -> None:
		try:
			deadline = time.monotonic() + 5
			addresses = resolve(host, port, self.stop, deadline)
			lastError = OSError("No server address resolved")
			for index, (family, kind, proto, _, address) in enumerate(addresses):
				if self.stop.is_set():
					raise OSError("Audio start cancelled")
				tcp = socket.socket(family, kind, proto)
				try:
					tcp.settimeout(max(0.01, (deadline - time.monotonic()) / (len(addresses) - index)))
					tcp.connect(address)
				except OSError as error:
					lastError = error
					tcp.close()
				else:
					self.tcp = tcp
					break
			if self.tcp is None:
				raise lastError
			self.tcp.settimeout(0.2)
			self.tcp.sendall(json.dumps({"role": role, "key": key}).encode("utf-8") + b"\n")
			responseBytes = bytearray()
			deadline = time.monotonic() + 5
			while not responseBytes.endswith(b"\n"):
				if self.stop.is_set() or time.monotonic() >= deadline:
					raise TimeoutError("Audio handshake timed out or cancelled")
				try:
					byte = self.tcp.recv(1)
				except socket.timeout:
					continue
				if not byte or len(responseBytes) >= 4096:
					raise ValueError("Missing or oversized audio handshake")
				responseBytes.extend(byte)
			raw = json.loads(responseBytes)
			if not isinstance(raw, dict):
				raise ValueError("Invalid audio handshake")
			response = cast(dict[str, Any], raw)
			if (
				response.get("status"),
				response.get("role"),
				response.get("key"),
			) != ("ok", role, key):
				raise ValueError("Audio handshake rejected or identity mismatched")
			identity = response.get("session_id")
			if not isinstance(identity, str) or len(identity) != 32:
				raise ValueError("Invalid session ID")
			self.identity = bytes.fromhex(identity)
			if len(self.identity) != 16:
				raise ValueError("Invalid session ID")
			udpPort = self._number(response, "udp_port", 1, 65535)
			_ = self._number(response, "udp_audio_payload_max_bytes", frameBytes, 1200)
			self.tcpInterval = max(
				1,
				self._number(response, "tcp_heartbeat_interval_ms", 1, 60000, 5000) / 1000,
			)
			udpTimeout = self._number(response, "udp_session_timeout_ms", 300, 300000, 15000) / 1000
			self.udpInterval = max(0.1, min(self.tcpInterval, udpTimeout / 3))
		except (OSError, ValueError, TypeError) as error:
			raise AudioError("control_connection_failed", str(error)) from error
		try:
			self.udp = socket.socket(self.tcp.family, socket.SOCK_DGRAM)
			address = list(self.tcp.getpeername())
			address[1] = udpPort
			self.udp.connect(tuple(address))
			self.udp.settimeout(0.1)
			register = b"RAS1\x01\x01" + self.identity
			deadline = time.monotonic() + 5
			nextRegister = 0
			while not self.stop.is_set() and time.monotonic() < deadline:
				if time.monotonic() >= nextRegister:
					_ = self.udp.send(register)
					nextRegister = time.monotonic() + 0.5
				try:
					packet = self.udp.recv(1401)
				except socket.timeout:
					continue
				if packet == b"RAS1\x01\x02" + self.identity:
					break
			else:
				raise TimeoutError("Audio UDP registration timed out or cancelled")
			self.nextTcp = self.nextUdp = time.monotonic()
		except OSError as error:
			raise AudioError("udp_registration_failed", str(error)) from error

	@staticmethod
	def _number(
		response: dict[str, Any],
		name: str,
		minimum: int,
		maximum: int,
		default: int | None = None,
	) -> int:
		value = response.get(name, default)
		if type(value) is not int or not minimum <= value <= maximum:
			raise ValueError(f"Invalid server field: {name}")
		return value

	def poll(self, timeout: float) -> bytes | None:
		assert self.tcp is not None and self.udp is not None
		try:
			now = time.monotonic()
			if now >= self.nextTcp:
				self.tcp.sendall(b'{"type":"heartbeat"}\n')
				self.nextTcp = now + self.tcpInterval
			if now >= self.nextUdp:
				_ = self.udp.send(b"RAS1\x01\x03" + self.identity)
				self.nextUdp = now + self.udpInterval
			readable, _, _ = select.select([self.tcp, self.udp], [], [], timeout)
			if self.tcp in readable:
				data = self.tcp.recv(4096)
				if not data:
					raise AudioError("control_connection_failed", "Audio server closed the connection")
				self.controlBuffer.extend(data)
				while b"\n" in self.controlBuffer:
					line, _, rest = self.controlBuffer.partition(b"\n")
					if len(line) > 4096 or not isinstance(json.loads(line), dict):
						raise ValueError("Invalid control response")
					self.controlBuffer = bytearray(rest)
				if len(self.controlBuffer) > 4096:
					raise ValueError("Oversized control response")
			if self.udp in readable:
				return self.udp.recv(1401)
		except (OSError, ValueError) as error:
			raise AudioError("audio_transport_failed", str(error)) from error
		return None

	def send(self, sequence: int, payload: bytes) -> None:
		assert self.udp is not None
		try:
			_ = self.udp.send(audioPacket(self.identity, sequence, payload))
		except OSError as error:
			raise AudioError("audio_transport_failed", str(error)) from error

	def close(self) -> None:
		for sock in (self.udp, self.tcp):
			if sock is not None:
				sock.close()
