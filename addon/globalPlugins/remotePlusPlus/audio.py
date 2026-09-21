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


if TYPE_CHECKING:
	from .audioRuntime import AudioRuntime

addonHandler.initTranslation()


AUDIO_PORT = 6838
AUDIO_PROTOCOL_VERSION = 2
AUDIO_ENVELOPE_KEY = "remotePlusPlus_audio"
AUDIO_SOURCE_SYSTEM = 1
AUDIO_SOURCE_MICROPHONE = 2
AUDIO_SOURCE_MASK = AUDIO_SOURCE_SYSTEM | AUDIO_SOURCE_MICROPHONE
AUDIO_REQUEST_TIMEOUT = 8.0
AUDIO_BUFFER_VALUES = (0, 10, 20, 40, 80)
AUDIO_BITRATES = (64, 96, 192)
AUDIO_CHANNELS = (1, 2)
AUDIO_FRAME_VALUES = (10, 20)


class AudioSettings(NamedTuple):
	"""Controller preferences; playback buffering is local to the listener."""

	bufferMs: int = 0
	bitrateKbps: int = 96
	channels: int = 2
	frameMs: int = 10

	def formatFields(self) -> dict[str, str | int]:
		return {
			"codec": "opus",
			"bitrate_kbps": self.bitrateKbps,
			"channels": self.channels,
			"frame_ms": self.frameMs,
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


def audioSettingsFromEnvelope(value: dict[str, Any]) -> AudioSettings | None:
	if value.get("codec") != "opus":
		return None
	for name, choices in (
		("bitrate_kbps", AUDIO_BITRATES),
		("channels", AUDIO_CHANNELS),
		("frame_ms", AUDIO_FRAME_VALUES),
	):
		if type(value.get(name)) is not int or value[name] not in choices:
			return None
	return AudioSettings(0, value["bitrate_kbps"], value["channels"], value["frame_ms"])


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
		envelope["sources"] = sources
	envelope.update(fields)
	return envelope


def parse_audio_envelope(value: Any) -> dict[str, Any] | None:
	"""Validate and return an audio control envelope from a remote payload."""
	if not isinstance(value, dict):
		return None
	value = cast(dict[str, Any], value)
	if type(value.get("version")) is not int or value["version"] not in (1, AUDIO_PROTOCOL_VERSION):
		return None
	kind = value.get("kind")
	if kind not in ("hello", "request", "response"):
		return None
	if value["version"] == 1 and kind != "request":
		return None
	request_id = value.get("request_id")
	if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 80):
		return None
	if kind in ("request", "response") and (
		request_id is None or "sources" not in value or normalize_source_mask(value["sources"]) is None
	):
		return None
	if "sources" in value and normalize_source_mask(value["sources"]) is None:
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


def createAudioRuntime(*args: Any) -> AudioRuntime:
	from .audioRuntime import AudioRuntime

	return AudioRuntime(*args)


class AudioService:
	"""Own audio workers for the active Remote session."""

	def __init__(self) -> None:
		self._lock = RLock()
		self._runtime: AudioRuntime | None = None
		self._threads: list[Thread] = []
		self._closed = False
		self._generation = 0
		self._state: AudioState = "off"
		self._error: str | None = None
		self._errorCode: str | None = None
		self._role: str | None = None
		self._sources = 0
		self._isReceiving = False
		self._muted = False
		self.settings = AudioSettings()
		self._ready = Event()
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
		"""Whether this subscriber is receiving valid PCM, independent of readiness."""
		with self._lock:
			return self._state == "on" and self._role == "subscriber" and self._isReceiving

	def set_state_callback(self, callback: Callable[[AudioStateEvent], None]) -> None:
		"""Set the callback invoked when audio worker state changes."""
		with self._lock:
			self._on_state_changed = callback

	def notify_state(self, state: AudioState, error: str | None = None) -> None:
		"""Notify the registered consumer of an externally detected state change."""
		with self._lock:
			notification = (AudioStateEvent(state, error, self._generation), self._on_state_changed)
		self._notify(notification)

	def setMuted(self, muted: bool) -> None:
		with self._lock:
			self._muted = muted
			runtime = self._runtime
		if runtime is not None:
			runtime.setMuted(muted)

	def start(
		self,
		host: str,
		mode: ConnectionMode,
		key: str,
		sources: int = AUDIO_SOURCE_SYSTEM,
		port: int = AUDIO_PORT,
		settings: AudioSettings = AudioSettings(),
	) -> bool:
		"""Start publisher or subscriber workers inside NVDA."""
		with self._lock:
			if self._closed or self._runtime is not None:
				return False
		if not isinstance(host, str) or not host.strip():
			self._set_error(_("Audio server host is unavailable."))
			return False
		if not isinstance(key, str) or not key or not 1 <= port <= 65535:
			self._set_error(_("Audio connection settings are invalid."))
			return False
		normalized_sources = normalize_source_mask(sources)
		if normalized_sources is None:
			self._set_error(_("Audio source selection is invalid."))
			return False
		if normalizeAudioSettings(settings._asdict()) != settings:
			self._set_error(_("Audio connection settings are invalid."))
			return False
		role = "subscriber" if mode == ConnectionMode.LEADER else "publisher"
		if role == "publisher" and normalized_sources == 0:
			self._set_error(_("No audio source is enabled."))
			return False

		with self._lock:
			if self._closed or self._runtime is not None:
				return False
			generation = self._generation
			try:
				runtime = createAudioRuntime(host.strip(), port, key, role, sources, settings, self._muted)
			except Exception:
				log.error("Unable to initialize audio workers", exc_info=True)
				notification = self._set_state_locked("error", _("Audio component failed."))
				runtime = None
			else:
				self._runtime = runtime
				self._generation += 1
				generation = self._generation
				self._role, self._sources = role, normalized_sources
				self.settings = settings
				self._ready = Event()
				notification = self._set_state_locked("starting", None)
		self._notify(notification)
		if runtime is None:
			return False
		try:
			with self._lock:
				if self._closed or self._runtime is not runtime:
					return False
				self._threads = [thread for thread in self._threads if thread.is_alive()]
				thread = Thread(
					target=self._run,
					args=(runtime, generation),
					name="remotePlusPlusAudio",
					daemon=True,
				)
				# Register only successfully started threads, before terminate can snapshot them.
				thread.start()
				self._threads.append(thread)
		except RuntimeError:
			runtime.stop()
			with self._lock:
				if self._runtime is runtime:
					self._runtime = None
			self._set_state("error", _("Audio component failed."), generation)
			return False
		return True

	def wait_until_ready(self, timeout: float = 6.0) -> bool:
		"""Wait for the current audio worker to report that its streams are ready."""
		with self._lock:
			generation = self._generation
			ready = self._ready
		_ = ready.wait(timeout)
		with self._lock:
			return generation == self._generation and self._state == "on"

	def stop(self) -> None:
		with self._lock:
			runtime = self._runtime
			self._generation += 1
			self._runtime = None
			self._role = None
			self._sources = 0
			self._ready.set()
			notification = self._set_state_locked("off", None)
		if runtime is not None:
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
		# Workers finish state callbacks under _lock; never join while holding it.
		# Include retired generations whose device cleanup is still in progress.
		for thread in threads:
			thread.join()
		with self._lock:
			self._threads.clear()

	def is_active(self) -> bool:
		return self.state in {"starting", "on"}

	def _event(self, event: dict[str, Any], generation: int) -> None:
		if event.get("type") == "ready":
			self._set_state("on", None, generation)
		elif event.get("type") == "media" and type(event.get("receiving")) is bool:
			with self._lock:
				if generation == self._generation:
					self._isReceiving = event["receiving"]
		elif event.get("type") == "error":
			code = event.get("code")
			log.error("Audio error (%s): %s", code, event.get("message"))
			self._set_state(
				"error",
				nativeAudioErrorMessage(code),
				generation,
				code if isinstance(code, str) else None,
			)

	def _run(self, runtime: AudioRuntime, generation: int) -> None:
		try:
			runtime.run(lambda event: self._event(event, generation))
		except Exception:
			log.error("Audio worker failed", exc_info=True)
			self._set_state("error", _("Audio component failed."), generation)
		finally:
			try:
				runtime.stop()
			except Exception:
				log.error("Failed to stop audio worker", exc_info=True)
			with self._lock:
				if generation != self._generation:
					return
				self._runtime = None
				self._role = None
				self._sources = 0
				self._ready.set()
				notification = None
				if self._state != "error":
					notification = self._set_state_locked("error", _("Audio component stopped unexpectedly."))
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
