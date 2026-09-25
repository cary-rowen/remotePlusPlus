# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2026 Cary-rowen <manchen_0528@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""Audio control protocol and in-process worker lifecycle."""

from __future__ import annotations

import addonHandler
from collections.abc import Callable
from threading import Event, RLock, Thread
from typing import Any, Literal, NamedTuple, TYPE_CHECKING, cast

from logHandler import log

from _remoteClient.connectionInfo import ConnectionMode

try:
	from .audioTransport import (
		STREAM_SYSTEM_AUDIO,
		STREAM_VOICE_CONTROLLED_TO_CONTROLLER,
		STREAM_VOICE_CONTROLLER_TO_CONTROLLED,
	)
except ImportError:
	# The module is also loaded standalone by the audio unit tests.
	STREAM_SYSTEM_AUDIO = "system_audio"
	STREAM_VOICE_CONTROLLED_TO_CONTROLLER = "voice_controlled_to_controller"
	STREAM_VOICE_CONTROLLER_TO_CONTROLLED = "voice_controller_to_controlled"


if TYPE_CHECKING:
	from .audioRuntime import AudioRuntime

addonHandler.initTranslation()


AUDIO_PORT = 6838
AUDIO_PROTOCOL_VERSION = 3
AUDIO_ENVELOPE_KEY = "remotePlusPlus_audio"
AUDIO_SOURCE_SYSTEM = 1
AUDIO_SOURCE_VOICE = 2
# Keep the internal bit value stable while exposing voice-call terminology.
AUDIO_SOURCE_MICROPHONE = AUDIO_SOURCE_VOICE
AUDIO_SOURCE_MASK = AUDIO_SOURCE_SYSTEM | AUDIO_SOURCE_MICROPHONE
AUDIO_REQUEST_TIMEOUT = 8.0
AUDIO_BUFFER_VALUES = (0, 10, 20, 40, 80)
AUDIO_BITRATES = (64, 96, 192)
AUDIO_CHANNELS = (1, 2)
AUDIO_FRAME_VALUES = (10, 20)


class AudioSettings(NamedTuple):
	"""Settings for one audio category."""

	bufferMs: int = 0
	bitrateKbps: int = 96
	channels: int = 2
	frameMs: int = 10

	def envelopeFields(self) -> dict[str, str | int]:
		return {
			"codec": "opus",
			"bitrate_kbps": self.bitrateKbps,
			"channels": self.channels,
			"frame_ms": self.frameMs,
			"buffer_ms": self.bufferMs,
		}


def normalizeAudioSettings(value: Any) -> AudioSettings:
	"""Recover each invalid saved preference independently using its default."""
	if not isinstance(value, dict):
		return AudioSettings()
	value = cast(dict[str, Any], value)
	bufferMs = value.get("bufferMs")
	bitrate = value.get("bitrateKbps")
	legacyMono = value.get("quality") in ("48000_mono", "24000_mono", "16000_mono")
	channels = value.get("channels", 1 if legacyMono else 2)
	frameMs = value.get("frameMs")
	return AudioSettings(
		bufferMs if type(bufferMs) is int and bufferMs in AUDIO_BUFFER_VALUES else 0,
		bitrate if type(bitrate) is int and bitrate in AUDIO_BITRATES else 96,
		channels if type(channels) is int and channels in AUDIO_CHANNELS else 2,
		frameMs if type(frameMs) is int and frameMs in AUDIO_FRAME_VALUES else 10,
	)


def audioSettingsFromEnvelope(value: Any) -> AudioSettings | None:
	if not isinstance(value, dict) or value.get("codec") != "opus":
		return None
	for name, choices in (
		("bitrate_kbps", AUDIO_BITRATES),
		("channels", AUDIO_CHANNELS),
		("frame_ms", AUDIO_FRAME_VALUES),
	):
		if type(value.get(name)) is not int or value[name] not in choices:
			return None
	bufferMs = value.get("buffer_ms", 0)
	if type(bufferMs) is not int or bufferMs not in AUDIO_BUFFER_VALUES:
		return None
	return AudioSettings(bufferMs, value["bitrate_kbps"], value["channels"], value["frame_ms"])


AudioState = Literal["off", "starting", "on", "error"]


class AudioStateEvent(NamedTuple):
	state: AudioState
	error: str | None
	generation: int
	errorCode: str | None = None


def nativeAudioErrorMessage(code: Any) -> str:
	"""Translate stable audio worker codes; unknown/older components use the fallback."""
	messages = {
		# Translators: The audio audio worker rejected its connection or audio settings.
		"invalid_settings": _("Audio connection settings are invalid."),
		"control_connection_failed": _(
			# Translators: Connecting to or communicating with the audio server failed.
			"Unable to communicate with the audio server. Check the server and connection key.",
		),
		"udp_registration_failed": _(
			# Translators: The audio server did not accept the UDP audio connection.
			"Unable to establish UDP audio transport. Check the audio server and firewall.",
		),
		# Translators: No default microphone is available on the computer capturing audio.
		"no_microphone": _("No default microphone is available."),
		# Translators: No default output device is available for playback or system audio capture.
		"no_output_device": _("No default audio output device is available."),
		# Translators: An audio device could not be opened or stopped working.
		"audio_device_failed": _("The audio device is unavailable or stopped working."),
		# Translators: Sending or receiving audio data failed.
		"audio_transport_failed": _("The audio connection failed."),
		# Translators: The bundled Opus library could not be loaded or used.
		"audio_codec_failed": _(
			"The Opus audio codec is unavailable or failed. Reinstall Remote++ on both computers.",
		),
		# Translators: The controlled computer stopped sharing audio, for example when reloading add-ons.
		"publisher_stopped": _("Audio sharing stopped on the controlled computer."),
	}
	# Translators: Fallback for an unknown error from the audio component.
	return messages.get(code if isinstance(code, str) else "", _("Audio component failed."))


def normalize_source_mask(value: Any) -> int | None:
	"""Return a valid source mask, or ``None`` for malformed input."""
	if isinstance(value, bool) or not isinstance(value, int):
		return None
	if value < 0 or value & ~AUDIO_SOURCE_MASK:
		return None
	return value


def make_audio_envelope(
	kind: str,
	*,
	request_id: str | None = None,
	system_audio: bool | None = None,
	voice_call: bool | None = None,
	system_audio_settings: AudioSettings | dict[str, Any] | None = None,
	voice_call_settings: AudioSettings | dict[str, Any] | None = None,
	sources: int | None = None,
	**fields: Any,
) -> dict[str, Any]:
	"""Build the payload carried by an NVDA Remote ``error`` message."""
	envelope: dict[str, Any] = {
		"version": AUDIO_PROTOCOL_VERSION,
		"kind": kind,
	}
	if request_id is not None:
		envelope["request_id"] = request_id
	if sources is not None:
		normalized = normalize_source_mask(sources)
		if normalized is None:
			raise ValueError("Invalid audio source mask")
		system_audio = bool(normalized & AUDIO_SOURCE_SYSTEM) if system_audio is None else system_audio
		voice_call = bool(normalized & AUDIO_SOURCE_VOICE) if voice_call is None else voice_call
		# This field is accepted only to keep existing in-process callers readable.
		envelope["sources"] = normalized
	if system_audio is not None:
		envelope["system_audio"] = system_audio
	if voice_call is not None:
		envelope["voice_call"] = voice_call
	if system_audio and system_audio_settings is None:
		system_audio_settings = AudioSettings()
	if voice_call and voice_call_settings is None:
		voice_call_settings = AudioSettings()
	for name, value in (
		("system_audio_settings", system_audio_settings),
		("voice_call_settings", voice_call_settings),
	):
		if value is not None:
			envelope[name] = value.envelopeFields() if isinstance(value, AudioSettings) else dict(value)
	envelope.update(fields)
	return envelope


def parse_audio_envelope(value: Any) -> dict[str, Any] | None:
	"""Validate and return an audio control envelope from a remote payload."""
	if not isinstance(value, dict):
		return None
	value = cast(dict[str, Any], value)
	if type(value.get("version")) is not int or value["version"] != AUDIO_PROTOCOL_VERSION:
		return None
	kind = value.get("kind")
	if kind not in ("hello", "request", "response", "device_changed"):
		return None
	request_id = value.get("request_id")
	if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 80):
		return None
	if kind in ("request", "response") and request_id is None:
		return None
	if kind in ("request", "response") and (
		type(value.get("system_audio")) is not bool or type(value.get("voice_call")) is not bool
	):
		return None
	if any(name in value for name in ("codec", "bitrate_kbps", "channels", "frame_ms", "buffer_ms")):
		return None
	if "sources" in value:
		sources = normalize_source_mask(value["sources"])
		declaredSources = audio_sources_from_envelope(value)
		if sources is None or declaredSources is None or sources != declaredSources:
			return None
	for name, enabled in (
		("system_audio_settings", value.get("system_audio")),
		("voice_call_settings", value.get("voice_call")),
	):
		if name in value and audioSettingsFromEnvelope(value[name]) is None:
			return None
		if enabled and name not in value:
			return None
		if not enabled and name in value:
			return None
	if kind == "response" and value.get("status") not in ("ok", "error"):
		return None
	if "message" in value and value["message"] is not None and not isinstance(value["message"], str):
		return None
	if value.get("error_code") is not None and (
		not isinstance(value["error_code"], str) or len(value["error_code"]) > 80
	):
		return None
	if "includes_nvda_speech" in value and type(value["includes_nvda_speech"]) is not bool:
		return None
	return dict(value)


def audio_sources_from_envelope(value: dict[str, Any]) -> int | None:
	if type(value.get("system_audio")) is not bool or type(value.get("voice_call")) is not bool:
		return None
	return (AUDIO_SOURCE_SYSTEM if value["system_audio"] else 0) | (
		AUDIO_SOURCE_VOICE if value["voice_call"] else 0
	)


def createAudioRuntime(*args: Any) -> AudioRuntime:
	from .audioRuntime import AudioRuntime

	return AudioRuntime(*args)


class AudioService:
	"""Own independent stream workers for the active Remote session."""

	def __init__(self) -> None:
		self._lock = RLock()
		self._runtimes: dict[str, AudioRuntime] = {}
		self._threads: list[Thread] = []
		self._closed = False
		self._generation = 0
		self._state: AudioState = "off"
		self._error: str | None = None
		self._errorCode: str | None = None
		self._role: str | None = None
		self._sources = 0
		self._isReceiving = False
		self._systemCaptureDeviceId: str | None = None
		self._muted = False
		self.settings = AudioSettings()
		self.voiceSettings = AudioSettings()
		self._ready = Event()
		self._readyStreams: set[str] = set()
		self._expectedReady = 0
		self._on_state_changed: Callable[[AudioStateEvent], None] | None = None

	@property
	def generation(self) -> int:
		with self._lock:
			return self._generation

	@property
	def state(self) -> AudioState:
		with self._lock:
			return self._state

	@property
	def error(self) -> str | None:
		with self._lock:
			return self._error

	@property
	def errorCode(self) -> str | None:
		with self._lock:
			return self._errorCode

	@property
	def role(self) -> str | None:
		with self._lock:
			return self._role

	@property
	def sources(self) -> int:
		with self._lock:
			return self._sources

	@property
	def isReceiving(self) -> bool:
		"""Whether the system-audio subscriber is receiving valid PCM."""
		with self._lock:
			return self._state == "on" and self._isReceiving

	@property
	def systemCaptureDeviceId(self) -> str | None:
		with self._lock:
			return self._systemCaptureDeviceId

	@property
	def captureDeviceIds(self) -> tuple[str | None, str | None]:
		with self._lock:
			system = self._runtimes.get(STREAM_SYSTEM_AUDIO)
			microphone = self._runtimes.get(STREAM_VOICE_CONTROLLED_TO_CONTROLLER)
			return (
				system.captureDeviceId if system is not None else None,
				microphone.captureDeviceId if microphone is not None else None,
			)

	def set_state_callback(self, callback: Callable[[AudioStateEvent], None]) -> None:
		with self._lock:
			self._on_state_changed = callback

	def notify_state(self, state: AudioState, error: str | None = None) -> None:
		with self._lock:
			notification = (AudioStateEvent(state, error, self._generation), self._on_state_changed)
		self._notify(notification)

	def setMuted(self, muted: bool) -> None:
		with self._lock:
			self._muted = muted
			runtimes = tuple(self._runtimes.values())
		for runtime in runtimes:
			runtime.setMuted(muted)

	def _streamSpecs(
		self,
		mode: ConnectionMode,
		sources: int,
		systemSettings: AudioSettings,
		voiceSettings: AudioSettings,
	) -> list[tuple[str, str, int, AudioSettings]]:
		if mode == ConnectionMode.LEADER:
			specs: list[tuple[str, str, int, AudioSettings]] = []
			if sources & AUDIO_SOURCE_SYSTEM:
				specs.append((STREAM_SYSTEM_AUDIO, "subscriber", AUDIO_SOURCE_SYSTEM, systemSettings))
			if sources & AUDIO_SOURCE_VOICE:
				specs.extend(
					(
						(
							STREAM_VOICE_CONTROLLED_TO_CONTROLLER,
							"subscriber",
							AUDIO_SOURCE_VOICE,
							voiceSettings,
						),
						(
							STREAM_VOICE_CONTROLLER_TO_CONTROLLED,
							"publisher",
							AUDIO_SOURCE_MICROPHONE,
							voiceSettings,
						),
					),
				)
			return specs
		specs: list[tuple[str, str, int, AudioSettings]] = []
		if sources & AUDIO_SOURCE_SYSTEM:
			specs.append((STREAM_SYSTEM_AUDIO, "publisher", AUDIO_SOURCE_SYSTEM, systemSettings))
		if sources & AUDIO_SOURCE_VOICE:
			specs.extend(
				(
					(
						STREAM_VOICE_CONTROLLED_TO_CONTROLLER,
						"publisher",
						AUDIO_SOURCE_MICROPHONE,
						voiceSettings,
					),
					(STREAM_VOICE_CONTROLLER_TO_CONTROLLED, "subscriber", AUDIO_SOURCE_VOICE, voiceSettings),
				),
			)
		return specs

	def start(
		self,
		host: str,
		mode: ConnectionMode,
		key: str,
		sources: int = AUDIO_SOURCE_SYSTEM,
		port: int = AUDIO_PORT,
		settings: AudioSettings = AudioSettings(),
		voiceSettings: AudioSettings | None = None,
		systemDeviceId: str | None = None,
		microphoneDeviceId: str | None = None,
	) -> bool:
		failedRuntimes = ()
		with self._lock:
			if self._closed or self._runtimes:
				return False
		if not isinstance(host, str) or not host.strip():
			self._set_error(_("Audio server host is unavailable."))
			return False
		if not isinstance(key, str) or not key or not 1 <= port <= 65535:
			self._set_error(_("Audio connection settings are invalid."))
			return False
		normalized_sources = normalize_source_mask(sources)
		voiceSettings = voiceSettings or settings
		if normalized_sources is None:
			self._set_error(_("Audio source selection is invalid."))
			return False
		if (
			normalizeAudioSettings(settings._asdict()) != settings
			or normalizeAudioSettings(voiceSettings._asdict()) != voiceSettings
		):
			self._set_error(_("Audio connection settings are invalid."))
			return False
		if normalized_sources == 0:
			self._set_error(_("No audio source is enabled."))
			return False
		specs = self._streamSpecs(mode, normalized_sources, settings, voiceSettings)
		role = "subscriber" if mode == ConnectionMode.LEADER else "publisher"

		with self._lock:
			if self._closed or self._runtimes:
				return False
			generation = self._generation + 1
			runtimes: dict[str, AudioRuntime] = {}
			try:
				for stream, runtimeRole, captureSources, streamSettings in specs:
					captureDeviceId = None
					if runtimeRole == "publisher":
						captureDeviceId = (
							systemDeviceId if stream == STREAM_SYSTEM_AUDIO else microphoneDeviceId
						)
					runtime = createAudioRuntime(
						host.strip(),
						port,
						key,
						runtimeRole,
						captureSources,
						streamSettings,
						self._muted,
						stream,
						captureDeviceId,
					)
					runtimes[stream] = runtime
			except Exception:
				log.error("Unable to initialize audio workers", exc_info=True)
				notification = self._set_state_locked("error", _("Audio component failed."))
				failedRuntimes = tuple(runtimes.values())
				runtimes = {}
			else:
				self._runtimes = runtimes
				self._generation = generation
				self._role, self._sources = role, normalized_sources
				self._systemCaptureDeviceId = None
				self.settings = settings
				self.voiceSettings = voiceSettings
				self._ready = Event()
				self._readyStreams = set()
				self._expectedReady = len(runtimes)
				notification = self._set_state_locked("starting", None)
		self._notify(notification)
		if not runtimes:
			for runtime in failedRuntimes:
				try:
					runtime.stop()
				except Exception:
					log.error("Failed to stop audio worker", exc_info=True)
			return False
		try:
			launched = False
			with self._lock:
				if not self._closed and self._generation == generation:
					self._threads = [thread for thread in self._threads if thread.is_alive()]
					launched = True
					for stream, runtime in runtimes.items():
						thread = Thread(
							target=self._run,
							args=(runtime, generation, stream),
							name=f"remotePlusPlusAudio_{stream}",
							daemon=True,
						)
						thread.start()
						self._threads.append(thread)
			if not launched:
				# A starting callback may have stopped this generation before its workers
				# were launched. Stop only workers still owned by this service.
				with self._lock:
					orphaned = tuple(
						runtime
						for stream, runtime in runtimes.items()
						if self._runtimes.get(stream) is runtime
					)
				for runtime in orphaned:
					runtime.stop()
				return False
		except RuntimeError:
			self.stop()
			self._set_error(_("Audio component failed."))
			return False
		return True

	def wait_until_ready(self, timeout: float = 6.0) -> bool:
		with self._lock:
			generation = self._generation
			ready = self._ready
		_ = ready.wait(timeout)
		with self._lock:
			return generation == self._generation and self._state == "on"

	def stop(self) -> None:
		with self._lock:
			runtimes = tuple(self._runtimes.values())
			self._generation += 1
			self._runtimes.clear()
			self._role = None
			self._sources = 0
			self._systemCaptureDeviceId = None
			self._ready.set()
			notification = self._set_state_locked("off", None)
		for runtime in runtimes:
			try:
				runtime.stop()
			except Exception:
				log.error("Failed to stop audio worker", exc_info=True)
		self._notify(notification)

	def terminate(self) -> None:
		with self._lock:
			self._closed = True
			threads = tuple(self._threads)
		self.stop()
		for thread in threads:
			thread.join()
		with self._lock:
			self._threads.clear()

	def is_active(self) -> bool:
		return self.state in {"starting", "on"}

	def _event(self, event: dict[str, Any], generation: int, stream: str) -> None:
		if event.get("type") == "capture_device" and stream == STREAM_SYSTEM_AUDIO:
			with self._lock:
				if generation == self._generation and isinstance(event.get("device_id"), str):
					self._systemCaptureDeviceId = event["device_id"]
		elif event.get("type") == "ready":
			with self._lock:
				if (
					generation != self._generation
					or self._state != "starting"
					or stream in self._readyStreams
				):
					return
				self._readyStreams.add(stream)
				ready = len(self._readyStreams) == self._expectedReady
			if ready:
				self._set_state("on", None, generation)
		elif event.get("type") == "media" and type(event.get("receiving")) is bool:
			with self._lock:
				if generation == self._generation and stream == STREAM_SYSTEM_AUDIO:
					self._isReceiving = event["receiving"]
		elif event.get("type") == "error":
			code = event.get("code")
			log.error("Audio error (%s): %s", code, event.get("message"))
			with self._lock:
				if generation != self._generation:
					return
				runtimes = tuple(self._runtimes.values())
			self._set_state(
				"error",
				nativeAudioErrorMessage(code),
				generation,
				code if isinstance(code, str) else None,
			)
			for runtime in runtimes:
				try:
					runtime.stop()
				except Exception:
					log.error("Failed to stop audio worker after an error", exc_info=True)

	def _run(self, runtime: AudioRuntime, generation: int, stream: str | None = None) -> None:
		stream = stream or STREAM_SYSTEM_AUDIO
		try:
			runtime.run(lambda event: self._event(event, generation, stream))
		except Exception:
			log.error("Audio worker failed", exc_info=True)
			with self._lock:
				if generation != self._generation:
					return
				runtimes = tuple(self._runtimes.values())
			self._set_state("error", _("Audio component failed."), generation)
			for other in runtimes:
				if other is not runtime:
					try:
						other.stop()
					except Exception:
						log.error("Failed to stop audio worker", exc_info=True)
		finally:
			try:
				runtime.stop()
			except Exception:
				log.error("Failed to stop audio worker", exc_info=True)
			with self._lock:
				if generation != self._generation:
					return
				self._runtimes.pop(stream, None)
				notification = None
				if not self._runtimes:
					self._role = None
					self._sources = 0
					self._ready.set()
					if self._state != "error":
						notification = self._set_state_locked(
							"error",
							_("Audio component stopped unexpectedly."),
						)
			self._notify(notification)

	def _set_state(
		self,
		state: AudioState,
		error: str | None,
		generation: int,
		errorCode: str | None = None,
	) -> None:
		with self._lock:
			if generation != self._generation:
				return
			if state in {"on", "error"}:
				self._ready.set()
			notification = self._set_state_locked(state, error, errorCode)
		self._notify(notification)

	def _set_error(self, error: str) -> None:
		with self._lock:
			notification = self._set_state_locked("error", error)
			self._ready.set()
		self._notify(notification)

	def _set_state_locked(
		self,
		state: AudioState,
		error: str | None,
		errorCode: str | None = None,
	) -> tuple[AudioStateEvent, Callable[[AudioStateEvent], None] | None]:
		self._state = state
		self._error = error
		self._errorCode = errorCode
		if state != "on":
			self._isReceiving = False
		return AudioStateEvent(state, error, self._generation, errorCode), self._on_state_changed

	@staticmethod
	def _notify(
		notification: tuple[AudioStateEvent, Callable[[AudioStateEvent], None] | None] | None,
	) -> None:
		if notification and notification[1]:
			try:
				notification[1](notification[0])
			except Exception:
				log.error("Audio state callback failed", exc_info=True)
