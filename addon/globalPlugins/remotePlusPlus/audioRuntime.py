"""Bounded capture/mixing and playback workers for one relay session."""

from __future__ import annotations
from collections import deque
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from typing import Any
import ctypes
from functools import partial
import math
import threading
import time
import traceback

from .audioTransport import AudioError, Session, STREAM_SYSTEM_AUDIO, UINT64_MASK, parseAudio
from .audioCom import clearExceptionFrames, comApartment
from .audioCodec import OpusCodec, RATE


def mixPcm(first: bytes, second: bytes) -> bytes:
	if len(first) != len(second) or len(first) % 2:
		raise ValueError("Mixing requires equally sized PCM16 frames")
	# Treat Python's arbitrary-precision integers as packed 16-bit lanes. Bias
	# signed samples to unsigned, then use (a & b) + ((a ^ b) >> 1) to average
	# without overflow. The mask removes bits shifted across lane boundaries.
	# All sample work runs in Python's native integer operations, outside a loop.
	sign = int.from_bytes(b"\x00\x80" * (len(first) // 2), "little")
	lower = sign - (sign >> 15)
	left = int.from_bytes(first, "little") ^ sign
	right = int.from_bytes(second, "little") ^ sign
	average = (left & right) + (((left ^ right) >> 1) & lower)
	return (average ^ sign).to_bytes(len(first), "little")


@contextmanager
def playback(rate: int, channels: int) -> Iterator[Any]:
	import nvwave

	with comApartment():
		player = None
		try:
			player = nvwave.WavePlayer(
				channels=channels,
				samplesPerSec=rate,
				bitsPerSample=16,
				outputDevice="",
				wantDucking=False,
				purpose=nvwave.AudioPurpose.SPEECH,
			)
			# 远程音频可能包含朗读，不跟随本机提示音音量，也不裁剪连续音频中的静音。
			player.enableTrimmingLeadingSilence(False)
			yield player
		finally:
			try:
				if player is not None:
					player.close()
			finally:
				player = None


class PlaybackBuffer:
	def __init__(self, bufferMs: int) -> None:
		self.target = max(1, bufferMs // 5)
		self.frames: deque[bytes] = deque(maxlen=bufferMs // 5 + 8)
		self.started = False
		self.underrunAt: float | None = None

	def clear(self) -> None:
		self.frames.clear()
		self.started = False
		self.underrunAt = None

	def pop(self, *, underrun: bool = True) -> bytes | None:
		if not self.frames:
			if underrun:
				if self.started:
					self.underrunAt = time.monotonic()
				self.started = False
			return None
		if not self.started:
			if len(self.frames) < self.target:
				return None
			self.started = True
		return self.frames.popleft()


class AudioRuntime:
	def __init__(
		self,
		host: str,
		port: int,
		key: str,
		role: str,
		sources: int,
		settings: Any,
		muted: bool,
		stream: str = STREAM_SYSTEM_AUDIO,
	) -> None:
		self.host, self.port, self.key = host, port, key
		self.role = role
		self.stream = stream
		self.rate = RATE
		self.channels = settings.channels
		self.bitrateKbps = settings.bitrateKbps
		self.frameMs = settings.frameMs
		self.frameBytes = self.rate * self.frameMs // 1000 * self.channels * 2
		self.playBytes = self.rate // 200 * self.channels * 2
		self.payloadBytes = self.bitrateKbps * self.frameMs // 8
		self.codec: OpusCodec | None = None
		self.stopping = threading.Event()
		# Queue and player state share one lock, including mute and stop.
		self.playLock = threading.RLock()
		self.condition = threading.Condition(self.playLock)
		self.player: Any = None
		self.muted = muted
		self.playEpoch = 0
		self.buffer = PlaybackBuffer(settings.bufferMs)
		self.captureBuffers = {source: bytearray() for source in (1, 2) if sources & source}
		self.lastCaptured = 0.0
		self.encoderTailSamples = 0
		self.workers: list[threading.Thread] = []
		self.error: Exception | None = None
		self.readyCount = 0
		self.nextSequence: int | None = None
		self.lastReceived = 0.0
		self.receiving = False
		self.session: Session | None = None

	def stop(self) -> None:
		self.stopping.set()
		if self.session is not None:
			self.session.close()
		self._clearPlayback()
		with self.condition:
			self.condition.notify_all()

	def setMuted(self, muted: bool) -> None:
		with self.playLock:
			self.muted = muted
			self._clearPlayback()

	def _clearPlayback(self) -> None:
		with self.playLock:
			with self.condition:
				self.playEpoch += 1
				self.buffer.clear()
				self.condition.notify_all()
			if self.player is not None:
				try:
					self.player.stop()
				except BaseException as error:
					# Other threads may stop playback. Release traceback-held player refs
					# before unlocking, so the owner can destroy it before COM shutdown.
					clearExceptionFrames(error)
					raise

	def _ready(self) -> None:
		with self.condition:
			self.readyCount += 1
			self.condition.notify_all()

	def _worker(self, target: Callable[..., None], *args: Any) -> None:
		try:
			target(*args)
		except Exception as error:
			with self.condition:
				if self.error is None:
					self.error = AudioError("audio_device_failed", "".join(traceback.format_exception(error)))
				self.condition.notify_all()

	def _startWorker(self, target: Callable[..., None], *args: Any) -> None:
		thread = threading.Thread(
			target=self._worker,
			args=(target, *args),
			daemon=True,
			name="remotePlusPlusAudioDevice",
		)
		self.workers.append(thread)
		thread.start()

	def _captured(self, source: int, pcm: bytes, discontinuity: bool) -> None:
		with self.condition:
			buffer = self.captureBuffers[source]
			if discontinuity:
				buffer.clear()
			buffer.extend(pcm)
			if pcm:
				self.lastCaptured = time.monotonic()
			# 40 ms per device, dropping whole sample frames on overflow.
			excess = len(buffer) - self.playBytes * 8
			if excess > 0:
				del buffer[:excess]

	def _mix(self) -> bytes | None:
		with self.condition:
			if self.stopping.is_set():
				return None
			available = min(self.frameBytes, max(map(len, self.captureBuffers.values()), default=0))
			if available < self.frameBytes:
				# WASAPI 空闲时可能不再回调；等待一个帧周期后补齐尾帧。
				if (
					not available and not self.encoderTailSamples
				) or time.monotonic() - self.lastCaptured < self.frameMs / 1000:
					return None
			if available and self.codec is not None:
				self.encoderTailSamples = self.codec.lookaheadSamples
			# 已补的零也会排出编码器尾音，避免自然停音后重复补帧。
			paddingSamples = (self.frameBytes - available) // (self.channels * 2)
			self.encoderTailSamples = max(0, self.encoderTailSamples - paddingSamples)
			frames: list[bytes] = []
			for buffer in self.captureBuffers.values():
				count = min(len(buffer), self.frameBytes)
				frames.append(bytes(buffer[:count]) + bytes(self.frameBytes - count))
				del buffer[:count]
		return frames[0] if len(frames) == 1 else mixPcm(*frames)

	def _receive(
		self,
		packet: bytes | None,
		identity: bytes,
		event: Callable[[dict[str, Any]], None],
	) -> None:
		now = time.monotonic()
		if self.receiving and now - self.lastReceived > 0.5:
			self.receiving = False
			self.nextSequence = None
			self._clearPlayback()
			if self.codec is not None:
				self.codec.reset()
			event({"type": "media", "receiving": False})
		parsed = parseAudio(packet, identity, self.payloadBytes) if packet else None
		if parsed is None or self.codec is None or self.stopping.is_set():
			return
		sequence, payload = parsed
		gap = (sequence - self.nextSequence) & UINT64_MASK if self.nextSequence is not None else 0
		if gap > UINT64_MASK // 2:
			return
		with self.condition:
			epoch = self.playEpoch
		try:
			if gap * self.frameMs > 40:
				self.codec.reset()
				pcm = self.codec.decode(payload)
				with self.playLock:
					if epoch == self.playEpoch:
						self._clearPlayback()
						epoch = self.playEpoch
			else:
				pcm = b"".join(self.codec.decode(None) for _ in range(gap)) + self.codec.decode(payload)
		except ValueError:
			# Malformed media cannot keep Remote speech suppressed.
			self.codec.reset()
			return
		self.nextSequence = (sequence + 1) & UINT64_MASK
		self.lastReceived = now
		with self.condition:
			if not self.muted and not self.stopping.is_set() and epoch == self.playEpoch:
				if self.buffer.underrunAt is not None:
					if 0 < gap * self.frameMs <= 40:
						# 声卡在途音频也已耗尽时，不补播已错过起播时间的 5 ms 块。
						elapsed = max(0, time.monotonic() - self.buffer.underrunAt)
						expired = min(gap * self.frameMs // 5, math.ceil(elapsed * 200))
						pcm = pcm[expired * self.playBytes :]
					self.buffer.underrunAt = None
				self.buffer.frames.extend(
					pcm[i : i + self.playBytes] for i in range(0, len(pcm), self.playBytes)
				)
				self.condition.notify_all()
		if not self.receiving:
			self.receiving = True
			event({"type": "media", "receiving": True})

	def _play(self) -> None:
		with playback(self.rate, self.channels) as player:
			with self.playLock:
				self.player = player
			self._ready()
			pending: deque[tuple[threading.Event, float]] = deque()
			epoch = self.playEpoch
			try:
				while not self.stopping.is_set():
					with self.playLock:
						if epoch != self.playEpoch:
							pending.clear()
							epoch = self.playEpoch
						# NVDA dispatches completion callbacks from feed/sync, not a separate thread.
						if pending:
							player.feed(b"")
							while pending and pending[0][0].is_set():
								_ = pending.popleft()
							if pending and time.monotonic() - pending[0][1] > 2:
								raise OSError("Audio playback made no progress")
						with self.condition:
							payload = (
								self.buffer.pop(underrun=not pending)
								if len(pending) < 4 and not self.muted
								else None
							)
						if payload is not None and not self.stopping.is_set():
							done = threading.Event()
							player.feed(payload, onDone=done.set)
							pending.append((done, time.monotonic()))
							continue
					with self.condition:
						_ = self.condition.wait(0.002 if pending else 0.02)
			finally:
				with self.playLock:
					self.player = None
				player = None

	def run(self, event: Callable[[dict[str, Any]], None]) -> None:
		if self.stopping.is_set():
			return
		# Windows socket/condition waits otherwise round 5 ms pacing to ~16 ms.
		# Balance the process-scoped multimedia timer request on every exit path.
		winmm = ctypes.WinDLL("winmm")
		timerStarted = winmm.timeBeginPeriod(1) == 0
		try:
			self._run(event)
		finally:
			if timerStarted:
				winmm.timeEndPeriod(1)

	def _run(self, event: Callable[[dict[str, Any]], None]) -> None:
		session = self.session = Session(self.stopping)
		try:
			if self.stopping.is_set():
				return
			self.codec = OpusCodec(
				self.channels,
				self.bitrateKbps,
				self.frameMs,
				encoder=self.role == "publisher",
			)
			session.open(self.host, self.port, self.key, self.role, self.payloadBytes, self.stream)
			if self.role == "publisher":
				from .audioCapture import capture

				for source in self.captureBuffers:
					self._startWorker(
						capture,
						self.stopping,
						source == 1,
						self.rate,
						self.channels,
						self._ready,
						partial(self._captured, source),
					)
			else:
				self._startWorker(self._play)
			deadline = time.monotonic() + 4
			with self.condition:
				while self.readyCount < len(self.workers) and not self.error and not self.stopping.is_set():
					if time.monotonic() >= deadline:
						raise AudioError("audio_device_failed", "Audio device startup timed out")
					_ = self.condition.wait(0.1)
			self._checkError()
			if self.stopping.is_set():
				return
			event({"type": "ready"})
			sequence = 0
			nextFrame = time.monotonic()
			while not self.stopping.is_set():
				self._checkError()
				packet = session.poll(
					max(0, nextFrame - time.monotonic()) if self.role == "publisher" else 0.05,
				)
				if self.role == "subscriber":
					self._receive(packet, session.identity, event)
				elif time.monotonic() >= nextFrame:
					now = time.monotonic()
					while nextFrame <= now and not self.stopping.is_set():
						payload = self._mix()
						if payload is None:
							nextFrame = now + self.frameMs / 1000
							break
						session.send(sequence, self.codec.encode(payload))
						sequence = (sequence + 1) & UINT64_MASK
						nextFrame += self.frameMs / 1000
		except Exception as error:
			if not self.stopping.is_set():
				event(
					{
						"type": "error",
						"code": getattr(error, "code", "audio_device_failed"),
						"message": str(error),
					},
				)
		finally:
			try:
				self.stop()
			finally:
				try:
					session.close()
					for worker in self.workers:
						worker.join()
				finally:
					if self.codec is not None:
						self.codec.close()
						self.codec = None

	def _checkError(self) -> None:
		if self.error is not None:
			raise AudioError("audio_device_failed", str(self.error)) from self.error
