"""Publish Linda's default PulseAudio/PipeWire sink to a Remote++ controller."""

import os
import select
import subprocess
import threading
import time

import globalPluginHandler
from logHandler import log
from _remoteClient.protocol import RemoteMessageType

from .audioCodec import OpusCodec
from .audioTransport import Session

ENVELOPE_KEY = "remotePlusPlus_audio"
PROTOCOL_VERSION = 3


def validSettings(value):
	return (
		isinstance(value, dict)
		and value.get("codec") == "opus"
		and all(
			type(value.get(field)) is int and value[field] in allowed
			for field, allowed in (
				("channels", (1, 2)),
				("bitrate_kbps", (64, 96, 192)),
				("frame_ms", (10, 20)),
			)
		)
	)


def readFrame(fd, size, stop):
	frame = bytearray()
	deadline = time.monotonic() + 2
	while not stop.is_set():
		if time.monotonic() >= deadline:
			raise TimeoutError("PulseAudio/PipeWire capture stalled")
		if not select.select([fd], [], [], 0.05)[0]:
			continue
		chunk = os.read(fd, size - len(frame))
		if not chunk:
			raise OSError("PulseAudio/PipeWire capture ended")
		frame.extend(chunk)
		if len(frame) == size:
			return bytes(frame)
	return None


class Publisher:
	def __init__(self, info, port, settings):
		self.info, self.port, self.settings = info, port, settings
		self.stopEvent = threading.Event()
		self.ready = threading.Event()
		self.error = None
		self.thread = threading.Thread(target=self._run, daemon=True, name="lindaRemoteAudio")

	def start(self):
		self.thread.start()

	def stop(self):
		self.stopEvent.set()

	def _run(self):
		codec, session, process = None, None, None
		try:
			settings = self.settings
			codec = OpusCodec(
				settings["channels"], settings["bitrate_kbps"], settings["frame_ms"], encoder=True
			)
			session = Session(self.stopEvent)
			session.open(
				self.info.hostname, self.port, self.info.key, "publisher", codec.payloadBytes, "system_audio"
			)
			process = subprocess.Popen(
				[
					"parec",
					"--device=@DEFAULT_MONITOR@",
					"--format=s16le",
					"--rate=48000",
					f"--channels={settings['channels']}",
					"--latency-msec=20",
				],
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
			)
			sequence = 0
			while not self.stopEvent.is_set():
				frame = readFrame(process.stdout.fileno(), codec.pcmBytes, self.stopEvent)
				if frame is None:
					break
				session.poll(0)
				session.send(sequence, codec.encode(frame))
				# Announce readiness only once a captured frame reached the audio server.
				self.ready.set()
				sequence = (sequence + 1) & ((1 << 64) - 1)
		except Exception as error:
			if not self.stopEvent.is_set():
				self.error = str(error)
				log.error("Linda remote audio publisher failed", exc_info=True)
		finally:
			self.ready.set()
			try:
				if process is not None:
					process.terminate()
					try:
						process.wait(timeout=1)
					except subprocess.TimeoutExpired:
						process.kill()
						process.wait()
					process.stdout.close()
			finally:
				try:
					if session is not None:
						session.close()
				finally:
					if codec is not None:
						codec.close()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self):
		super().__init__()
		self.transport = self.session = self.publisher = self.owner = None
		self.activeRequest = None
		self.retired = []
		self.lock = threading.RLock()
		self.closed = threading.Event()
		self.requests = []
		self.requestReady = threading.Event()
		self.worker = threading.Thread(target=self._work, daemon=True, name="lindaRemoteAudioRequests")
		self.watcher = threading.Thread(target=self._watch, daemon=True, name="lindaRemoteAudioWatch")
		self.worker.start()
		self.watcher.start()

	def terminate(self):
		self.closed.set()
		self.requestReady.set()
		self.watcher.join()
		with self.lock:
			self._attach(None)
		self.worker.join(timeout=12)
		self._waitRetired()
		super().terminate()

	def _stopPublisher(self):
		if self.publisher is not None:
			self.publisher.stop()
			self.retired.append(self.publisher)
		self.publisher = self.owner = None
		self.activeRequest = None

	def _waitRetired(self):
		with self.lock:
			retired = list(self.retired)
		for publisher in retired:
			if publisher.thread.ident is not None:
				publisher.thread.join(timeout=6)
			if publisher.thread.is_alive():
				return False
		with self.lock:
			self.retired = [publisher for publisher in self.retired if publisher not in retired]
		return True

	def _checkPublisher(self):
		with self.lock:
			if self.publisher is None or not self.publisher.error or self.activeRequest is None:
				return
			transport, origin, requestId = self.activeRequest
			self._stopPublisher()
			self._send(
				transport,
				origin,
				{
					"version": PROTOCOL_VERSION,
					"kind": "response",
					"request_id": requestId,
					"system_audio": False,
					"voice_call": False,
					"includes_nvda_speech": False,
					"status": "error",
					"error_code": "audio_device_failed",
					"message": "Linux audio capture failed.",
				},
			)

	def _attach(self, session):
		if self.transport is not None:
			self.transport.unregisterInbound(RemoteMessageType.ERROR, self._handle)
			self.transport.unregisterInbound(RemoteMessageType.CLIENT_LEFT, self._peerLeft)
		self._stopPublisher()
		self.requests.clear()
		self.session = session
		self.transport = session.transport if session else None
		if self.transport is not None:
			self.transport.registerInbound(RemoteMessageType.ERROR, self._handle)
			self.transport.registerInbound(RemoteMessageType.CLIENT_LEFT, self._peerLeft)

	def _watch(self):
		import _remoteClient

		while not self.closed.wait(0.5):
			try:
				client = _remoteClient._remoteClient
				session = client.followerSession if client else None
				with self.lock:
					if session is not self.session:
						self._attach(session)
					elif session and session.transport is not self.transport:
						self._attach(session)
					elif self.transport is not None and not self.transport.connected:
						self._stopPublisher()
				self._checkPublisher()
			except Exception:
				log.error("Linda remote audio connection watcher failed", exc_info=True)

	def _peerLeft(self, client=None, user_id=None, **kwargs):
		peer = client.get("id") if isinstance(client, dict) else user_id
		with self.lock:
			if type(peer) is int and peer == self.owner:
				self._stopPublisher()

	def _handle(self, **payload):
		envelope = payload.get(ENVELOPE_KEY)
		origin = payload.get("origin")
		if (
			not isinstance(envelope, dict)
			or envelope.get("version") != PROTOCOL_VERSION
			or envelope.get("kind") != "request"
			or not isinstance(envelope.get("request_id"), str)
			or not 0 < len(envelope["request_id"]) <= 128
			or type(envelope.get("system_audio")) is not bool
			or type(envelope.get("voice_call")) is not bool
			or type(origin) is not int
		):
			return
		with self.lock:
			if self.closed.is_set() or not self.session or origin not in self.session.leaders:
				return
			# Bound pending requests and process them in order with one worker.
			if len(self.requests) < 16:
				self.requests.append((self.transport, origin, dict(envelope)))
				self.requestReady.set()

	def _send(self, transport, origin, envelope):
		with self.lock:
			if (
				not self.closed.is_set()
				and transport is self.transport
				and transport.connected
				and self.session
				and origin in self.session.leaders
			):
				transport.send(RemoteMessageType.ERROR, **{ENVELOPE_KEY: envelope})

	def _work(self):
		while not self.closed.is_set():
			self.requestReady.wait(0.5)
			self._waitRetired()
			with self.lock:
				if not self.requests:
					self.requestReady.clear()
					continue
				transport, origin, envelope = self.requests.pop(0)
			try:
				self._request(transport, origin, envelope)
			except Exception:
				log.error("Linda remote audio request failed", exc_info=True)

	def _request(self, transport, origin, envelope):
		base = {
			"version": PROTOCOL_VERSION,
			"kind": "response",
			"request_id": envelope["request_id"],
			"system_audio": False,
			"voice_call": False,
			"includes_nvda_speech": False,
			"status": "ok",
		}
		publisher = None
		info = None
		with self.lock:
			if (
				self.closed.is_set()
				or transport is not self.transport
				or not transport.connected
				or not self.session
				or origin not in self.session.leaders
			):
				return
			settings = envelope.get("system_audio_settings")
			if self.owner not in (None, origin):
				base.update(status="error", message="Another controller is using remote audio.")
			elif envelope["voice_call"]:
				base.update(
					status="error", error_code="no_microphone", message="Linux voice calls are not supported."
				)
			elif envelope["system_audio"] and not validSettings(settings):
				base.update(
					status="error", error_code="invalid_settings", message="Unsupported Opus audio settings."
				)
			else:
				self._stopPublisher()
				if envelope["system_audio"]:
					port = envelope.get("port", 6838)
					if type(port) is not int or not 1 <= port <= 65535:
						return
					info = self.session.getConnectionInfo()
		if info is not None:
			# The server allows only one publisher per room/stream. Finish old cleanup first.
			if not self._waitRetired():
				base.update(
					status="error",
					error_code="audio_device_failed",
					message="Previous audio capture is stopping.",
				)
			else:
				with self.lock:
					if (
						self.closed.is_set()
						or transport is not self.transport
						or not transport.connected
						or not self.session
						or origin not in self.session.leaders
					):
						return
					try:
						publisher = Publisher(info, port, settings)
						publisher.start()
						self.publisher, self.owner = publisher, origin
						self.activeRequest = (transport, origin, envelope["request_id"])
					except Exception:
						publisher = None
						base.update(
							status="error",
							error_code="audio_device_failed",
							message="Linux audio capture failed.",
						)
						log.error("Linda remote audio start failed", exc_info=True)
		if publisher is not None:
			ready = publisher.ready.wait(6)
			with self.lock:
				if self.publisher is not publisher or self.closed.is_set():
					return
				if not ready or publisher.error or not publisher.thread.is_alive():
					self._stopPublisher()
					base.update(
						status="error",
						error_code="audio_device_failed",
						message="Linux audio capture failed.",
					)
				else:
					base.update(system_audio=True, system_audio_settings=settings, includes_nvda_speech=True)
				self._send(transport, origin, base)
				return
		self._send(transport, origin, base)
