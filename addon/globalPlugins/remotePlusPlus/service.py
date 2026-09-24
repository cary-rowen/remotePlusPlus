# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""Business logic service for NVDA Remote PlusPlus.

Handles interaction with the core _remoteClient module, configuration reading,
and connection state management. This module should remain UI-agnostic.
"""

from __future__ import annotations

import addonHandler
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from extensionPoints import callWithSupportedKwargs
from functools import partial
import threading
import time
from typing import Any
import json
import os
import uuid
import config
import synthDriverHandler
import queueHandler
from logHandler import log
import globalVars
import _remoteClient
from _remoteClient.connectionInfo import ConnectionInfo, ConnectionMode
from _remoteClient.protocol import RemoteMessageType, addressToHostPort
from config.configFlags import RemoteConnectionMode

from .audio import (
	AUDIO_ENVELOPE_KEY,
	AUDIO_PORT,
	AUDIO_PROTOCOL_VERSION,
	AUDIO_REQUEST_TIMEOUT,
	AUDIO_SOURCE_SYSTEM,
	AUDIO_SOURCE_VOICE,
	AudioService,
	AudioSettings,
	AudioStateEvent,
	audio_sources_from_envelope,
	audioSettingsFromEnvelope,
	make_audio_envelope,
	nativeAudioErrorMessage,
	normalizeAudioSettings,
	normalize_source_mask,
	parse_audio_envelope,
)

addonHandler.initTranslation()


class ConnectionManager:
	"""Manages saved remote connections and groups."""

	DEFAULT_GROUP = "Default"
	CONFIG_FILENAME = "remotePlusPlus_connections.json"

	def __init__(self) -> None:
		self._configPath = os.path.join(globalVars.appArgs.configPath, self.CONFIG_FILENAME)
		self.data = self._getDefaultData()
		self.loadConfig()

	def _getDefaultData(self) -> dict[str, Any]:
		return {
			"active_group": self.DEFAULT_GROUP,
			"close_on_connect": True,
			"groups": {self.DEFAULT_GROUP: []},
		}

	def loadConfig(self) -> None:
		"""Load connections from disk.

		Reads the JSON config file and updates the internal data dictionary.
		Creates the default group if it doesn't exist after loading.
		"""
		if not os.path.exists(self._configPath):
			return

		try:
			with open(self._configPath, "r", encoding="utf-8") as f:
				loaded = json.load(f)
				self.data.update(loaded)

				if self.DEFAULT_GROUP not in self.data["groups"]:
					self.data["groups"][self.DEFAULT_GROUP] = []
				# Audio is session-scoped now. Ignore the legacy per-connection
				# settings while keeping the rest of an existing connection intact.
				for connections in self.data["groups"].values():
					if isinstance(connections, list):
						for connection in connections:
							if isinstance(connection, dict):
								connection.pop("audio", None)

		except (OSError, json.JSONDecodeError):
			log.error(f"Failed to load remote connections from {self._configPath}", exc_info=True)

	def saveConfig(self) -> bool:
		"""Save connections to disk atomically.

		Writes data to a temporary file first, then atomically replaces
		the target file to prevent corruption on failure.
		"""
		tmpPath = self._configPath + ".tmp"
		try:
			with open(tmpPath, "w", encoding="utf-8") as f:
				json.dump(self.data, f, indent=2, ensure_ascii=False)
			# Atomic replace
			os.replace(tmpPath, self._configPath)
			return True

		except OSError:
			log.error(f"Failed to save remote connections to {self._configPath}", exc_info=True)
			if os.path.exists(tmpPath):
				try:
					os.remove(tmpPath)
				except OSError:
					pass
			return False

	def getAudioSettings(self) -> AudioSettings:
		"""Return system-audio preferences, independent of saved connections."""
		return normalizeAudioSettings(self.data.get("audio_settings"))

	def setAudioSettings(self, settings: AudioSettings, voiceSettings: AudioSettings | None = None) -> bool:
		"""Persist preferences, restoring the previous values on write failure."""
		if normalizeAudioSettings(settings._asdict()) != settings or (
			voiceSettings is not None and normalizeAudioSettings(voiceSettings._asdict()) != voiceSettings
		):
			return False
		old = self.data.get("audio_settings")
		oldVoice = self.data.get("voice_audio_settings")
		self.data["audio_settings"] = settings._asdict()
		if voiceSettings is not None:
			self.data["voice_audio_settings"] = voiceSettings._asdict()
		if self.saveConfig():
			return True
		if old is None:
			self.data.pop("audio_settings", None)
		else:
			self.data["audio_settings"] = old
		if voiceSettings is not None:
			if oldVoice is None:
				self.data.pop("voice_audio_settings", None)
			else:
				self.data["voice_audio_settings"] = oldVoice
		return False

	def getVoiceAudioSettings(self) -> AudioSettings:
		"""Return voice-call preferences, defaulting to the system settings."""
		value = self.data.get("voice_audio_settings")
		if value is None:
			return self.getAudioSettings()
		return normalizeAudioSettings(value)

	def setVoiceAudioSettings(self, settings: AudioSettings) -> bool:
		if normalizeAudioSettings(settings._asdict()) != settings:
			return False
		old = self.data.get("voice_audio_settings")
		self.data["voice_audio_settings"] = settings._asdict()
		if self.saveConfig():
			return True
		if old is None:
			self.data.pop("voice_audio_settings", None)
		else:
			self.data["voice_audio_settings"] = old
		return False

	def getCloseOnConnect(self) -> bool:
		"""Return whether to close the dialog after connecting."""
		return self.data.get("close_on_connect", True)

	def setCloseOnConnect(self, value: bool) -> None:
		"""Set the close-on-connect preference.

		:param value: If True, the dialog will close after connecting.
		"""
		self.data["close_on_connect"] = value
		self.saveConfig()

	def getGroups(self) -> list[str]:
		"""Return a list of all group names."""
		return list(self.data["groups"].keys())

	def getActiveGroup(self) -> str:
		"""Return the name of the last active group."""
		group = self.data.get("active_group", self.DEFAULT_GROUP)
		if group not in self.data["groups"]:
			return self.DEFAULT_GROUP
		return group

	def setActiveGroup(self, groupName: str) -> None:
		"""Set the active group.

		:param groupName: The name of the group to activate.
		"""
		if groupName in self.data["groups"]:
			self.data["active_group"] = groupName
			self.saveConfig()

	def createGroup(self, groupName: str) -> bool:
		"""Create a new group.

		:param groupName: The name of the new group.
		:return: True if successful, False if the group already exists.
		"""
		groupName = groupName.strip()
		if not groupName:
			return False
		if groupName in self.data["groups"]:
			return False
		self.data["groups"][groupName] = []
		self.saveConfig()
		return True

	def renameGroup(self, oldName: str, newName: str) -> bool:
		"""Rename a group.

		:param oldName: The current name of the group.
		:param newName: The new name for the group.
		:return: True if successful, False if the group cannot be renamed.
		"""
		newName = newName.strip()
		if not newName:
			return False
		if oldName == self.DEFAULT_GROUP:
			return False
		if oldName not in self.data["groups"] or newName in self.data["groups"]:
			return False

		self.data["groups"][newName] = self.data["groups"].pop(oldName)
		if self.data["active_group"] == oldName:
			self.data["active_group"] = newName
		self.saveConfig()
		return True

	def deleteGroup(self, groupName: str, moveItemsToDefault: bool = True) -> bool:
		"""Delete a group.

		:param groupName: The name of the group to delete.
		:param moveItemsToDefault: If True, move connections to the default group.
		:return: True if successful, False if the group cannot be deleted.
		"""
		if groupName == self.DEFAULT_GROUP:
			return False
		if groupName not in self.data["groups"]:
			return False

		if moveItemsToDefault:
			items = self.data["groups"][groupName]
			self.data["groups"][self.DEFAULT_GROUP].extend(items)

		del self.data["groups"][groupName]
		if self.data["active_group"] == groupName:
			self.data["active_group"] = self.DEFAULT_GROUP
		self.saveConfig()
		return True

	def getConnections(self, groupName: str) -> list[dict[str, Any]]:
		"""Return connections for a given group.

		:param groupName: The name of the group.
		:return: A list of connection dictionaries, or an empty list if not found.
		"""
		return self.data["groups"].get(groupName, [])

	def getAdjacentConnection(
		self,
		currentId: str | None,
		direction: int,
	) -> dict[str, Any] | None:
		"""Return the next or previous saved connection after the selected entry."""
		connections: list[Any] = self.getConnections(self.getActiveGroup())
		if not connections:
			return None

		step = 1 if direction >= 0 else -1
		currentIndex = next(
			(
				index
				for index, connection in enumerate(connections)
				if isinstance(connection, dict) and connection.get("id") == currentId
			),
			-1,
		)

		if currentIndex == -1:
			return connections[0 if step > 0 else -1]
		if len(connections) == 1:
			return None
		return connections[(currentIndex + step) % len(connections)]

	def addConnection(
		self,
		groupName: str,
		name: str,
		host: str,
		key: str,
		port: int = 6837,
		mode: str = "leader",
		selfHosted: bool = False,
	) -> str | None:
		"""Add a connection to a group.

		:param groupName: The name of the group to add the connection to.
		:param name: The display name for the connection.
		:param host: The hostname or IP address of the server.
		:param key: The connection key.
		:param port: The port number, defaults to 6837.
		:param mode: Connection mode, 'leader' or 'follower'.
		:param selfHosted: If True, this is a locally hosted server.
		:return: The new connection ID, or None if the group doesn't exist.
		"""
		if groupName not in self.data["groups"]:
			return None

		newId = str(uuid.uuid4())
		entry = {
			"id": newId,
			"name": name,
			"host": host,
			"key": key,
			"port": port,
			"mode": mode,
			"selfHosted": selfHosted,
		}
		self.data["groups"][groupName].append(entry)
		self.saveConfig()
		return newId

	def updateConnection(self, groupName: str, connId: str, **kwargs: Any) -> bool:
		"""Update a connection's properties.

		:param groupName: The name of the group containing the connection.
		:param connId: The ID of the connection to update.
		:param kwargs: Key-value pairs of properties to update.
		:return: True if successful, False if not found.
		"""
		if groupName not in self.data["groups"]:
			return False

		connections = self.data["groups"][groupName]
		for conn in connections:
			if conn["id"] == connId:
				conn.update(kwargs)
				self.saveConfig()
				return True
		return False

	def deleteConnection(self, groupName: str, connId: str) -> bool:
		"""Delete a connection.

		:param groupName: The name of the group containing the connection.
		:param connId: The ID of the connection to delete.
		:return: True if successful, False if not found.
		"""
		if groupName not in self.data["groups"]:
			return False

		connections = self.data["groups"][groupName]
		for i, conn in enumerate(connections):
			if conn["id"] == connId:
				del connections[i]
				self.saveConfig()
				return True
		return False

	def swapConnections(self, groupName: str, firstConnId: str, secondConnId: str) -> bool:
		"""Swap two connections within a group.

		:param groupName: The name of the group containing the connections.
		:param firstConnId: The ID of the first connection.
		:param secondConnId: The ID of the second connection.
		:return: True if successful, False if either connection is not found.
		"""
		if groupName not in self.data["groups"] or firstConnId == secondConnId:
			return False

		connections = self.data["groups"][groupName]
		firstIdx = next((i for i, c in enumerate(connections) if c["id"] == firstConnId), -1)
		secondIdx = next((i for i, c in enumerate(connections) if c["id"] == secondConnId), -1)
		if firstIdx == -1 or secondIdx == -1:
			return False

		connections[firstIdx], connections[secondIdx] = connections[secondIdx], connections[firstIdx]
		self.saveConfig()
		return True


class RemoteService:
	"""Encapsulates NVDA Remote business logic."""

	def __init__(self) -> None:
		self.connection_manager = ConnectionManager()
		self._selectedSavedConnectionId: str | None = None
		self.audio = AudioService()
		self._audioStateCallback: Callable[[AudioStateEvent], None] | None = None
		self.audio.set_state_callback(self._onNativeAudioState)
		self._audioTransport: Any = None
		self._audioDisconnectCallback: Callable[[], None] | None = None
		self._audioRequestLock = threading.RLock()
		self._pendingAudioRequests: dict[str, tuple[threading.Timer, int, AudioSettings]] = {}
		self._pendingAudioVoiceSettings: dict[str, AudioSettings] = {}
		self._audioWorker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remotePlusPlusAudio")
		self._audioClosed = False
		self._audioEpoch = 0
		self._audioSettingsChanged = False
		self._publisherOwner: int | None = None
		self._audioPeerId: int | None = None
		self._activeAudioRequestId: str | None = None
		self._suppressedSpeechSession: Any = None
		self._speechReplayFilters: dict[RemoteMessageType, Callable] = {}
		self._speechSuppressionGeneration = 0
		self._remoteAudioIncludesSpeech = False
		self._publisherSpeechAvailable = False
		self._audioFollowers: set[int] = set()
		self._originalAudioSend: Callable | None = None
		self._audioSendWrapper: Callable | None = None
		self._mutedClient: Any = None
		self._originalToggleMute: Callable | None = None
		self._remoteAudioSources = 0
		self._audioRequestPending = False
		synthDriverHandler.synthChanged.register(self._handlePublisherSynthChanged)

	def isRunning(self) -> bool:
		"""Check if NVDA Remote client is running."""
		return _remoteClient.remoteRunning()

	def isConnected(self) -> bool:
		"""Check if there is an active remote connection."""
		if not self.isRunning():
			return False
		return _remoteClient._remoteClient.isConnected()

	def isConnecting(self) -> bool:
		"""Check if NVDA Remote is establishing or retrying a connection."""
		client = self.getClient()
		return bool(client and client.isConnecting)

	def getClient(self) -> "_remoteClient.client.RemoteClient | None":
		"""Return the raw RemoteClient instance if running, else None."""
		if self.isRunning():
			return _remoteClient._remoteClient
		return None

	def getCurrentConnectionInfo(self) -> ConnectionInfo | None:
		"""Return ConnectionInfo for the active session, if any."""
		client = self.getClient()
		if not client:
			return None
		session = client.leaderSession or client.followerSession
		if not session:
			return None

		info = session.getConnectionInfo()
		info.insecure = session.transport.insecure
		return info

	def getControlServerConfig(self) -> dict[str, Any] | None:
		"""Retrieve the 'controlServer' section from Remote config."""
		return _remoteClient.configuration.getRemoteConfig().get("controlServer")

	def isAutoConnectConfigured(self) -> bool:
		"""Check if auto-connect parameters are valid in config."""
		conf = self.getControlServerConfig()
		if not conf:
			return False

		return bool(
			conf.get("autoconnect", False)
			and conf.get("key")
			and (conf.get("host") or conf.get("selfHosted")),
		)

	def isCurrentConnectionDefault(self) -> bool:
		"""Check if the currently active connection matches the default auto-connect config."""
		if not self.isConnected():
			return False

		conf = self.getControlServerConfig()
		if not conf:
			return False

		currentInfo = self.getCurrentConnectionInfo()
		if not currentInfo:
			return False

		configHostname = "localhost"
		configPort = conf.get("port", 6837)

		if not conf.get("selfHosted", False):
			try:
				configHostname, configPort = addressToHostPort(conf["host"])
			except ValueError:
				return False

		configMode = RemoteConnectionMode(conf["connectionMode"]).toConnectionMode()

		return (
			currentInfo.hostname == configHostname
			and currentInfo.port == configPort
			and currentInfo.key == conf["key"]
			and currentInfo.mode == configMode
		)

	def isSelfHostedConnection(self, info: ConnectionInfo) -> bool:
		"""Check if a connection is to a locally hosted server.

		:param info: The ConnectionInfo to check.
		:return: True if it's a local (insecure localhost) connection.
		"""
		return info.insecure and info.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}

	def disconnect(self, silent: bool = False) -> None:
		"""Disconnect the current session.

		:param silent: If True, suppress user notifications during disconnect.
		"""
		self.stopAudio()
		client = self.getClient()
		if client:
			client.disconnect(_silent=silent)

	def setAudioStateCallback(self, callback: Callable[[AudioStateEvent], None]) -> None:
		self._audioStateCallback = callback

	def _onNativeAudioState(self, event: AudioStateEvent) -> None:
		with self._audioRequestLock:
			if self._audioClosed or event.generation != self.audio.generation:
				return
			state = event.state
			self._setRemoteSpeechSuppressed(
				state == "on"
				and self.audio.state == "on"
				and self.audio.role == "subscriber"
				and bool(self.audio.sources & AUDIO_SOURCE_SYSTEM),
			)
			if state == "error":
				if self._publisherOwner is not None and self._activeAudioRequestId is not None:
					self._queueAudioTask(
						self._handlePublisherAudioError,
						self._activeAudioRequestId,
						self._audioEpoch,
						event,
					)
				elif self.isAudioLeader():
					if self._remoteAudioSources or self._audioRequestPending:
						self._cancelRemoteAudio()
					self._remoteAudioSources = 0
					self._activeAudioRequestId = None
			if self._audioStateCallback is not None:
				self._audioStateCallback(event)

	def _setRemoteSpeechSuppressed(self, suppressed: bool) -> None:
		with self._audioRequestLock:
			self._speechSuppressionGeneration += 1
			generation = self._speechSuppressionGeneration
		if threading.current_thread() is not threading.main_thread():
			queueHandler.queueFunction(
				queueHandler.eventQueue,
				self._applyRemoteSpeechSuppressed,
				suppressed,
				generation,
			)
			return
		self._applyRemoteSpeechSuppressed(suppressed, generation)

	def _applyRemoteSpeechSuppressed(self, suppressed: bool, generation: int) -> None:
		"""Filter the publisher's audio messages, leaving other peers, mute and braille intact."""
		with self._audioRequestLock:
			if generation != self._speechSuppressionGeneration:
				return
			if suppressed:
				if self._suppressedSpeechSession is not None:
					return
				client = self.getClient()
				session = client.leaderSession if client else None
				if session is None or session.transport is not self._audioTransport:
					return
				self._suppressedSpeechSession = session
			else:
				session = self._suppressedSpeechSession
				if session is None:
					return
				self._suppressedSpeechSession = None
			# CANCEL and PAUSE must still reach any speech started during a media gap.
			for messageType, handler in (
				(RemoteMessageType.SPEAK, session.localMachine.speak),
				(RemoteMessageType.TONE, session.localMachine.beep),
				(RemoteMessageType.WAVE, session.localMachine.playWave),
			):
				if suppressed:
					filtered = partial(self._replayRemoteSpeech, handler)
					# Remote's extension points keep only weak references to callbacks.
					self._speechReplayFilters[messageType] = filtered
					session.transport.unregisterInbound(messageType, handler)
					session.transport.registerInbound(messageType, filtered)
				else:
					session.transport.unregisterInbound(
						messageType,
						self._speechReplayFilters.pop(messageType),
					)
					session.transport.registerInbound(messageType, handler)

	def _replayRemoteSpeech(self, handler: Callable, origin: int | None = None, **kwargs: Any) -> None:
		# Check reception for each message: an enabled listener may be waiting for
		# audio, and UDP can stop while both control connections remain healthy.
		# Coverage travels with speech so a volume change cannot race a separate
		# response queued on the audio worker. Older publishers use the handshake.
		includesSpeech = kwargs.pop("remotePlusPlus_speechInAudio", self._remoteAudioIncludesSpeech)
		if includesSpeech is True and origin == self._audioPeerId and self.audio.isReceiving:
			return
		callWithSupportedKwargs(handler, **kwargs)

	def _sendRemoteAudioMessage(self, send: Callable, type: RemoteMessageType, **kwargs: Any) -> None:
		if (
			send == self._originalAudioSend
			and type == RemoteMessageType.SPEAK
			and self._publisherOwner is not None
			and self.audio.state == "on"
		):
			kwargs["remotePlusPlus_speechInAudio"] = self._publisherIncludesSpeech(self.audio.sources)
		send(type, **kwargs)

	def _toggleRemoteMute(self) -> None:
		self._originalToggleMute()
		self.audio.setMuted(bool(self._mutedClient.localMachine.isMuted))

	def _publisherIncludesSpeech(self, sources: int) -> bool:
		"""Read native synth state only on the main thread; workers use its last result."""
		if not sources & AUDIO_SOURCE_SYSTEM:
			return False
		if threading.current_thread() is not threading.main_thread():
			return self._publisherSpeechAvailable
		# NVDA and WASAPI capture follow the default eConsole endpoint. For explicitly selected
		# devices, keep Remote speech even if that device happens to be default now.
		synth = synthDriverHandler.getSynth()
		self._publisherSpeechAvailable = bool(
			synth is not None
			and synth.name != "silence"
			and (not synth.isSupported("volume") or synth.volume > 0)
			and config.conf["audio"]["outputDevice"]
			== config.conf.getConfigValidation(("audio", "outputDevice")).default,
		)
		return self._publisherSpeechAvailable

	def _handlePublisherSynthChanged(self, **kwargs: Any) -> None:
		# Refresh even while a start request is queued and has not acquired ownership.
		self._publisherIncludesSpeech(AUDIO_SOURCE_SYSTEM)
		with self._audioRequestLock:
			if self._publisherOwner is not None and self._activeAudioRequestId is not None:
				self._queueAudioTask(
					self._sendPublisherSpeechState,
					self._activeAudioRequestId,
					self._audioEpoch,
				)

	def _sendPublisherSpeechState(self, requestId: str, epoch: int) -> None:
		with self._audioRequestLock:
			if (
				epoch != self._audioEpoch
				or requestId != self._activeAudioRequestId
				or self.audio.state != "on"
			):
				return
			self._sendAudioMessage(
				make_audio_envelope(
					"response",
					request_id=requestId,
					system_audio=bool(self.audio.sources & AUDIO_SOURCE_SYSTEM),
					voice_call=bool(self.audio.sources & AUDIO_SOURCE_VOICE),
					system_audio_settings=self.audio.settings
					if self.audio.sources & AUDIO_SOURCE_SYSTEM
					else None,
					voice_call_settings=(
						getattr(self.audio, "voiceSettings", self.audio.settings)
						if self.audio.sources & AUDIO_SOURCE_VOICE
						else None
					),
					status="ok",
					includes_nvda_speech=self._publisherIncludesSpeech(self.audio.sources),
				),
			)

	def _handlePublisherAudioError(
		self,
		requestId: str,
		epoch: int,
		event: AudioStateEvent,
	) -> None:
		with self._audioRequestLock:
			if (
				epoch != self._audioEpoch
				or requestId != self._activeAudioRequestId
				or event.generation != self.audio.generation
			):
				return
			self._sendAudioMessage(
				make_audio_envelope(
					"response",
					request_id=requestId,
					system_audio=False,
					voice_call=False,
					status="error",
					message=event.error,
					error_code=event.errorCode,
				),
			)
			self.stopAudio(notifyPublisher=False)

	def _cancelRemoteAudio(self) -> None:
		"""Best-effort release after timeout, playback failure, or local unload."""
		if self._audioTransport is not None:
			self._sendAudioMessage(
				make_audio_envelope(
					"request",
					request_id=uuid.uuid4().hex,
					system_audio=False,
					voice_call=False,
				),
			)

	def _queueAudioTask(self, task: Callable, *args: Any) -> None:
		with self._audioRequestLock:
			if not self._audioClosed:
				self._audioWorker.submit(self._runAudioTask, task, *args)

	def _runAudioTask(self, task: Callable, *args: Any) -> None:
		try:
			task(*args)
		except Exception:
			log.error("Remote++ audio task failed", exc_info=True)
			self.stopAudio(error=_("Audio component failed."))

	def getAudioSources(self) -> int:
		return self._remoteAudioSources if self.audio.is_active() or self._audioRequestPending else 0

	def applyAudioSettings(self) -> None:
		"""Apply saved preferences only to an existing controller audio request."""
		if not self.isAudioLeader():
			return
		with self._audioRequestLock:
			if self._audioRequestPending:
				self._audioSettingsChanged = True
				return
			sources = self.getAudioSources()
		if sources:
			self.requestAudioSources(sources)

	def isAudioRequestPending(self) -> bool:
		return self._audioRequestPending

	def isAudioLeader(self) -> bool:
		info = self.getCurrentConnectionInfo()
		return bool(info and info.mode == ConnectionMode.LEADER)

	def _getAudioTransport(self) -> Any:
		client = self.getClient()
		if not client:
			return None
		session = client.leaderSession or client.followerSession
		return session.transport if session else None

	def _sendAudioMessage(self, envelope: dict[str, Any]) -> bool:
		transport = self._audioTransport
		if transport is None or not transport.connected:
			transport = self._getAudioTransport()
		if transport is None or not transport.connected:
			return False
		try:
			transport.send(RemoteMessageType.ERROR, **{AUDIO_ENVELOPE_KEY: envelope})
		except (OSError, RuntimeError, TypeError):
			log.debug("Unable to send Remote++ audio message", exc_info=True)
			return False
		return True

	def requestAudioSources(self, sources: int) -> bool:
		"""Request system audio and/or a bidirectional voice call."""
		normalized = normalize_source_mask(sources)
		info = self.getCurrentConnectionInfo()
		if normalized is None or not info or info.mode != ConnectionMode.LEADER:
			return False
		if normalized and len(self._audioFollowers) > 1:
			# A rejected request must leave any existing audio session intact.
			if self._audioStateCallback is not None:
				self._audioStateCallback(
					AudioStateEvent(
						"error",
						# Translators: The relay cannot select among multiple controlled computers.
						_("Remote audio requires exactly one controlled computer in the channel."),
						self.audio.generation,
					),
				)
			return False
		with self._audioRequestLock:
			if self._audioRequestPending or self._audioClosed:
				return False
			requestId = uuid.uuid4().hex
			timer = threading.Timer(
				AUDIO_REQUEST_TIMEOUT,
				self._audioRequestTimedOut,
				args=(requestId,),
			)
			timer.daemon = True
			settings = self.connection_manager.getAudioSettings()
			voiceSettings = self.connection_manager.getVoiceAudioSettings()
			self._pendingAudioRequests[requestId] = (timer, normalized, settings)
			self._pendingAudioVoiceSettings[requestId] = voiceSettings
			self._audioRequestPending = True
			self._remoteAudioSources = normalized
			self._audioSettingsChanged = False
			self._activeAudioRequestId = None
			self._remoteAudioIncludesSpeech = False
			epoch = self._audioEpoch
		self._queueAudioTask(self._sendAudioRequest, requestId, normalized, settings, voiceSettings, epoch)
		return True

	def _sendAudioRequest(
		self,
		requestId: str,
		sources: int,
		settings: AudioSettings,
		voiceSettings: AudioSettings | None = None,
		epoch: int | None = None,
	) -> None:
		# Keep the old positional shape usable by in-process callers while all new
		# requests carry explicit system and voice settings.
		if epoch is None:
			epoch = voiceSettings if isinstance(voiceSettings, int) else self._audioEpoch
			voiceSettings = self.connection_manager.getVoiceAudioSettings()
		assert voiceSettings is not None
		with self._audioRequestLock:
			if epoch != self._audioEpoch:
				return
			# Stop playback before changing the publisher's format. A fresh UDP session
			# also keeps delayed packets from the previous format out of the new stream.
			self.audio.stop()
			if epoch != self._audioEpoch:
				return
			sent = self._sendAudioMessage(
				make_audio_envelope(
					"request",
					request_id=requestId,
					system_audio=bool(sources & AUDIO_SOURCE_SYSTEM),
					voice_call=bool(sources & AUDIO_SOURCE_VOICE),
					system_audio_settings=settings if sources & AUDIO_SOURCE_SYSTEM else None,
					voice_call_settings=voiceSettings if sources & AUDIO_SOURCE_VOICE else None,
					port=AUDIO_PORT,
				),
			)
			if sent:
				pending = self._pendingAudioRequests.get(requestId)
				if pending is not None:
					pending[0].start()
				return
			self._pendingAudioRequests.pop(requestId, None)
			self._pendingAudioVoiceSettings.pop(requestId, None)
			self._audioRequestPending = False
		self.audio.notify_state("error", _("The remote audio control channel is unavailable."))

	def _audioRequestTimedOut(self, requestId: str) -> None:
		with self._audioRequestLock:
			if requestId not in self._pendingAudioRequests:
				return
			self._cancelRemoteAudio()
			self._pendingAudioRequests.pop(requestId)[0].cancel()
			self._pendingAudioVoiceSettings.pop(requestId, None)
			self._audioRequestPending = False
			self._remoteAudioSources = 0
			# Translators: Audio needs the built-in Remote Access connection on both computers.
			error = _(
				"No audio response. Check the connection and make sure both computers use NVDA's "
				"built-in Remote Access with the same Remote++ version.",
			)
			self.audio.notify_state("error", error)

	def _handleAudioMessage(self, **payload: Any) -> None:
		envelope = parse_audio_envelope(payload.get(AUDIO_ENVELOPE_KEY))
		if envelope is None:
			return
		kind = envelope.get("kind")
		origin = payload.get("origin")
		if type(origin) is not int:
			return
		epoch = self._audioEpoch
		if kind == "request":
			# Remote dispatches inbound messages on the main thread, before worker handoff.
			sources = audio_sources_from_envelope(envelope)
			if sources is None:
				return
			self._publisherIncludesSpeech(sources)
			self._queueAudioTask(self._handleAudioRequest, envelope, origin, epoch)
		elif kind == "response":
			self._queueAudioTask(self._handleAudioResponse, envelope, origin, epoch)

	def _handleAudioRequest(self, envelope: dict[str, Any], origin: int, epoch: int) -> None:
		if epoch != self._audioEpoch:
			return
		requestId = envelope.get("request_id")
		sources = audio_sources_from_envelope(envelope)
		if not isinstance(requestId, str) or sources is None:
			return
		info = self.getCurrentConnectionInfo()
		if not info or info.mode != ConnectionMode.FOLLOWER:
			return
		client = self.getClient()
		if not client or not client.followerSession or origin not in client.followerSession.leaders:
			return
		systemSettings = audioSettingsFromEnvelope(envelope.get("system_audio_settings"))
		voiceSettings = audioSettingsFromEnvelope(envelope.get("voice_call_settings"))
		error = None
		errorCode = None
		if self._publisherOwner not in {None, origin} and self.audio.is_active():
			# Translators: Another controller currently owns the shared audio stream.
			error = _("Another controller is using remote audio. Try again after they turn it off.")
		elif sources and envelope.get("version") != AUDIO_PROTOCOL_VERSION:
			# Translators: PCM audio from older Remote++ versions is no longer supported.
			error = _("Upgrade Remote++ on both computers to use Opus audio.")
		elif sources & AUDIO_SOURCE_SYSTEM and systemSettings is None:
			# Translators: The peer requested invalid or unsupported Opus settings.
			error = _("The requested Opus audio settings are not supported.")
		elif sources & AUDIO_SOURCE_VOICE and voiceSettings is None:
			# Translators: The peer requested invalid or unsupported Opus settings.
			error = _("The requested Opus audio settings are not supported.")
		else:
			if sources == 0:
				self._activeAudioRequestId = None
				self.audio.stop()
				self._publisherOwner = None
			elif not (
				self.audio.state == "on"
				and self.audio.sources == sources
				and (not sources & AUDIO_SOURCE_SYSTEM or self.audio.settings == systemSettings)
				and (not sources & AUDIO_SOURCE_VOICE or self.audio.voiceSettings == voiceSettings)
			):
				assert systemSettings is not None or voiceSettings is not None
				self._activeAudioRequestId = None
				self.audio.stop()
				if epoch != self._audioEpoch:
					return
				with self._audioRequestLock:
					if epoch != self._audioEpoch:
						return
					self._publisherOwner = origin
					# Bind ownership and worker generation before accepting audio callbacks.
					self._activeAudioRequestId = requestId
					started = self.audio.start(
						info.hostname,
						ConnectionMode.FOLLOWER,
						info.key,
						sources=sources,
						settings=systemSettings or AudioSettings(),
						voiceSettings=voiceSettings or systemSettings or AudioSettings(),
					)
				deadline = time.monotonic() + 6
				while started and self.audio.state == "starting" and epoch == self._audioEpoch:
					if self.audio.wait_until_ready(0.1) or time.monotonic() >= deadline:
						break
				if epoch != self._audioEpoch:
					self.audio.stop()
					return
				if self.audio.state != "on":
					error = self.audio.error or _("Audio component unavailable.")
					errorCode = self.audio.errorCode
					self._activeAudioRequestId = None
					self.audio.stop()
					self._publisherOwner = None
			else:
				self._activeAudioRequestId = requestId
				if self.audio.state == "error":
					self._onNativeAudioState(
						AudioStateEvent(
							"error",
							self.audio.error,
							self.audio.generation,
							self.audio.errorCode,
						),
					)
		if epoch != self._audioEpoch:
			return
		if not self._sendAudioMessage(
			make_audio_envelope(
				"response",
				request_id=requestId,
				system_audio=bool(sources & AUDIO_SOURCE_SYSTEM),
				voice_call=bool(sources & AUDIO_SOURCE_VOICE),
				system_audio_settings=systemSettings if sources & AUDIO_SOURCE_SYSTEM else None,
				voice_call_settings=voiceSettings if sources & AUDIO_SOURCE_VOICE else None,
				status="error" if error else "ok",
				message=error,
				error_code=errorCode,
				includes_nvda_speech=not error and self._publisherIncludesSpeech(sources),
			),
		):
			if self._publisherOwner == origin:
				self._activeAudioRequestId = None
				self.audio.stop()
				self._publisherOwner = None

	def _handleAudioResponse(self, envelope: dict[str, Any], origin: int, epoch: int) -> None:
		if epoch != self._audioEpoch or not self.isAudioLeader():
			return
		if origin not in self._audioFollowers:
			return
		requestId = envelope.get("request_id")
		if not isinstance(requestId, str):
			return
		with self._audioRequestLock:
			pending = self._pendingAudioRequests.pop(requestId, None)
			if pending is None:
				if origin != self._audioPeerId or requestId != self._activeAudioRequestId:
					return
				if envelope.get("status") == "error":
					self.stopAudio(error=self._remoteAudioErrorMessage(envelope))
				elif (
					envelope.get("status") == "ok"
					and audio_sources_from_envelope(envelope) == self._remoteAudioSources
				):
					self._remoteAudioIncludesSpeech = envelope.get("includes_nvda_speech") is True
					self._setRemoteSpeechSuppressed(
						self.audio.state == "on" and bool(self.audio.sources & AUDIO_SOURCE_SYSTEM),
					)
				return
			timer, requestedSources, settings = pending
			voiceSettings = self._pendingAudioVoiceSettings.pop(
				requestId,
				self.connection_manager.getVoiceAudioSettings(),
			)
			timer.cancel()
		try:
			with self._audioRequestLock:
				self._startAudioResponse(envelope, origin, epoch, requestedSources, settings, voiceSettings)
		finally:
			with self._audioRequestLock:
				if epoch == self._audioEpoch:
					self._audioRequestPending = False
					if self._audioSettingsChanged and self.audio.is_active():
						self.applyAudioSettings()

	def _startAudioResponse(
		self,
		envelope: dict[str, Any],
		origin: int,
		epoch: int,
		requestedSources: int,
		settings: AudioSettings,
		voiceSettings: AudioSettings,
	) -> None:
		if epoch != self._audioEpoch:
			return
		status = envelope.get("status")
		sources = audio_sources_from_envelope(envelope)
		negotiatedSystem = audioSettingsFromEnvelope(envelope.get("system_audio_settings"))
		negotiatedVoice = audioSettingsFromEnvelope(envelope.get("voice_call_settings"))
		if sources is None:
			self.stopAudio(error=self._remoteAudioErrorMessage(envelope))
			return
		if (
			status != "ok"
			or sources != requestedSources
			or envelope.get("version") != AUDIO_PROTOCOL_VERSION
			or (sources & AUDIO_SOURCE_SYSTEM and negotiatedSystem != settings)
			or (sources & AUDIO_SOURCE_VOICE and negotiatedVoice != voiceSettings)
		):
			self.stopAudio(error=self._remoteAudioErrorMessage(envelope))
			return
		self._audioPeerId = origin
		info = self.getCurrentConnectionInfo()
		if sources == 0:
			self._remoteAudioSources = 0
			self.audio.stop()
			return
		if not info:
			self.audio.notify_state("error", _("Remote connection information is unavailable."))
			return
		if epoch == self._audioEpoch and not self.audio.is_active():
			self._activeAudioRequestId = envelope["request_id"]
			self._remoteAudioIncludesSpeech = envelope.get("includes_nvda_speech") is True
			client = self.getClient()
			if client is None:
				self.audio.notify_state("error", _("Remote connection information is unavailable."))
				return
			self.audio.setMuted(bool(client.localMachine.isMuted))
			started = self.audio.start(
				info.hostname,
				ConnectionMode.LEADER,
				info.key,
				sources=sources,
				port=AUDIO_PORT,
				settings=settings,
				voiceSettings=voiceSettings,
			)
			if not started:
				self.audio.notify_state("error", self.audio.error or _("Audio component unavailable."))
				return
		if epoch == self._audioEpoch:
			self._remoteAudioSources = sources

	@staticmethod
	def _remoteAudioErrorMessage(envelope: dict[str, Any]) -> str:
		if envelope.get("error_code") is not None:
			return nativeAudioErrorMessage(envelope["error_code"])
		return str(envelope.get("message") or _("Remote audio is unavailable."))

	def _registerAudioTransport(self, transport: Any) -> None:
		if transport is self._audioTransport:
			return
		if self._audioTransport is not None:
			self.stopAudio()
		self._unregisterAudioTransport()
		if transport is not None:
			client = self.getClient()
			if client and client.leaderSession and client.leaderSession.transport is transport:
				# When attaching to an already connected session, no snapshot is replayed.
				self._audioFollowers = set(client.leaderSession.followers)
				staleFollowers = getattr(transport, "_remotePlusPlusStaleFollowers", None)
				if staleFollowers is not None and staleFollowers[0] == transport.successfulConnects:
					self._audioFollowers.difference_update(staleFollowers[1])
				self._mutedClient = client
				self._originalToggleMute = client._doToggleMute
				client._doToggleMute = self._toggleRemoteMute
			self._originalAudioSend = transport.send
			self._audioSendWrapper = partial(self._sendRemoteAudioMessage, transport.send)
			transport.send = self._audioSendWrapper
			transport.registerInbound(RemoteMessageType.ERROR, self._handleAudioMessage)
			transport.registerInbound(RemoteMessageType.CLIENT_LEFT, self._handleAudioPeerLeft)
			transport.registerInbound(RemoteMessageType.CLIENT_JOINED, self._handleAudioPeerJoined)
			transport.registerInbound(RemoteMessageType.CHANNEL_JOINED, self._handleAudioChannelJoined)
			self._audioTransport = transport
			self._audioDisconnectCallback = partial(self._handleAudioTransportDisconnected, transport)
			transport.transportDisconnected.register(self._audioDisconnectCallback)

	def _unregisterAudioTransport(self) -> None:
		transport = self._audioTransport
		self._audioTransport = None
		self._audioFollowers.clear()
		if transport is not None:
			if transport.send is self._audioSendWrapper:
				transport.send = self._originalAudioSend
			self._originalAudioSend = None
			self._audioSendWrapper = None
			if self._mutedClient is not None:
				if self._mutedClient._doToggleMute == self._toggleRemoteMute:
					self._mutedClient._doToggleMute = self._originalToggleMute
				self._mutedClient = None
				self._originalToggleMute = None
			if self._audioDisconnectCallback is not None:
				transport.transportDisconnected.unregister(self._audioDisconnectCallback)
				self._audioDisconnectCallback = None
			try:
				transport.unregisterInbound(RemoteMessageType.ERROR, self._handleAudioMessage)
				transport.unregisterInbound(RemoteMessageType.CLIENT_LEFT, self._handleAudioPeerLeft)
				transport.unregisterInbound(RemoteMessageType.CLIENT_JOINED, self._handleAudioPeerJoined)
				transport.unregisterInbound(RemoteMessageType.CHANNEL_JOINED, self._handleAudioChannelJoined)
			except (KeyError, ValueError):
				pass

	def _handleAudioTransportDisconnected(self, transport: Any) -> None:
		with self._audioRequestLock:
			if transport is self._audioTransport:
				# Keep handlers for this transport's automatic reconnect; audio stays off.
				self.stopAudio()
				self._audioFollowers.clear()

	def _handleAudioChannelJoined(self, clients: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
		with self._audioRequestLock:
			self._audioFollowers = {
				client["id"]
				for client in clients or []
				if isinstance(client, dict)
				and type(client.get("id")) is int
				and client.get("connection_type") == ConnectionMode.FOLLOWER.value
			}
			client = self.getClient()
			if client and client.leaderSession and client.leaderSession.transport is self._audioTransport:
				# Core retains old IDs across reconnects. Keep only confirmed exclusions on
				# the transport for plugin reloads; core still tracks subsequent joins/leaves.
				self._audioTransport._remotePlusPlusStaleFollowers = (
					self._audioTransport.successfulConnects,
					set(client.leaderSession.followers) - self._audioFollowers,
				)
			if (self._audioPeerId is not None and self._audioPeerId not in self._audioFollowers) or (
				len(self._audioFollowers) > 1 and (self._remoteAudioSources or self._audioRequestPending)
			):
				self.stopAudio()

	def _handleAudioPeerJoined(self, client: dict[str, Any] | None = None, **kwargs: Any) -> None:
		if (
			isinstance(client, dict)
			and type(client.get("id")) is int
			and client.get("connection_type") == ConnectionMode.FOLLOWER.value
		):
			with self._audioRequestLock:
				self._audioFollowers.add(client["id"])
				if len(self._audioFollowers) > 1 and (self._remoteAudioSources or self._audioRequestPending):
					self.stopAudio()

	def _handleAudioPeerLeft(
		self,
		client: dict[str, Any] | None = None,
		user_id: int | None = None,
		**kwargs,
	) -> None:
		peerId = client.get("id") if isinstance(client, dict) else user_id
		if type(peerId) is int:
			with self._audioRequestLock:
				self._audioFollowers.discard(peerId)
				if peerId in {self._publisherOwner, self._audioPeerId}:
					self.stopAudio()

	def stopAudio(self, *, notifyPublisher: bool = True, error: str | None = None) -> None:
		with self._audioRequestLock:
			self._setRemoteSpeechSuppressed(False)
			if (
				notifyPublisher
				and self._publisherOwner is not None
				and self._activeAudioRequestId is not None
			):
				self._sendAudioMessage(
					make_audio_envelope(
						"response",
						request_id=self._activeAudioRequestId,
						system_audio=False,
						voice_call=False,
						status="error",
						error_code="publisher_stopped",
						message=nativeAudioErrorMessage("publisher_stopped"),
					),
				)
			if self.isAudioLeader() and (self._remoteAudioSources or self._audioRequestPending):
				self._cancelRemoteAudio()
			self._audioEpoch += 1
			for timer, _, _ in self._pendingAudioRequests.values():
				timer.cancel()
			self._pendingAudioRequests.clear()
			self._pendingAudioVoiceSettings.clear()
			self._audioRequestPending = False
			self._remoteAudioSources = 0
			self._audioSettingsChanged = False
			self._publisherOwner = None
			self._audioPeerId = None
			self._activeAudioRequestId = None
			self._remoteAudioIncludesSpeech = False
			self._queueAudioTask(self._stopNativeAudio, error)

	def _stopNativeAudio(self, error: str | None) -> None:
		self.audio.stop()
		with self._audioRequestLock:
			if error and not self._audioClosed and self._audioStateCallback is not None:
				# Report after stop advances the generation, so the queued UI callback
				# can distinguish this failure from a subsequently restarted worker.
				self._audioStateCallback(AudioStateEvent("error", error, self.audio.generation))

	def terminate(self) -> None:
		"""Invalidate pending work and release the single audio lifecycle worker."""
		synthDriverHandler.synthChanged.unregister(self._handlePublisherSynthChanged)
		self.handleRemoteConnectionChanged(False)
		with self._audioRequestLock:
			self._audioClosed = True
		try:
			self._audioWorker.shutdown(wait=True)
		finally:
			self.audio.terminate()

	def handleRemoteConnectionChanged(self, connected: bool) -> None:
		if not connected:
			self.stopAudio()
			self._unregisterAudioTransport()
			return
		self._registerAudioTransport(self._getAudioTransport())

	def connect(self, info: ConnectionInfo, savedConnectionId: str | None = None) -> None:
		"""Initiate a connection.

		:param info: The ConnectionInfo containing connection details.
		:param savedConnectionId: ID of the saved connection being used, if any.
		"""
		self._selectedSavedConnectionId = savedConnectionId if isinstance(savedConnectionId, str) else None
		client = self.getClient()
		if client:
			client.connect(info)

	def getAdjacentSavedConnection(self, direction: int) -> dict[str, Any] | None:
		"""Return an adjacent saved connection in the active group."""
		return self.connection_manager.getAdjacentConnection(self._selectedSavedConnectionId, direction)

	def connectSavedConnection(self, connection: dict[str, Any]) -> bool:
		"""Disconnect and initiate a connection from a saved connection entry."""
		try:
			mode = {
				"leader": ConnectionMode.LEADER,
				"follower": ConnectionMode.FOLLOWER,
			}[connection["mode"]]
			hostname = connection["host"]
			port = connection["port"]
			key = connection["key"]
			selfHosted = connection.get("selfHosted", False)
			if (
				type(hostname) is not str
				or not hostname
				or type(port) is not int
				or not 1 <= port <= 65535
				or type(key) is not str
				or not key
				or type(selfHosted) is not bool
			):
				raise ValueError
		except (KeyError, TypeError, ValueError):
			connectionDetails = (
				{field: connection.get(field) for field in ("host", "port", "mode", "selfHosted")}
				if isinstance(connection, dict)
				else type(connection).__name__
			)
			log.error(f"Invalid saved remote connection: {connectionDetails!r}")
			return False

		client = self.getClient()
		if client is None or client.isConnecting:
			return False
		info = ConnectionInfo(
			mode=mode,
			hostname=hostname,
			port=port,
			key=key,
			insecure=selfHosted,
		)
		if self.isConnected():
			self.disconnect(silent=True)
		if selfHosted:
			self.startLocalServer(port, key)
		self.connect(info, savedConnectionId=connection.get("id"))
		return True

	def startLocalServer(self, port: int, key: str) -> None:
		"""Start the local control server.

		:param port: The port number to listen on.
		:param key: The connection key for authentication.
		"""
		client = self.getClient()
		if client:
			client.startControlServer(port, key)

	def performAutoConnect(self) -> None:
		"""Trigger the auto-connect sequence based on config.

		Reads the control server configuration and initiates a connection.
		Starts a local server first if selfHosted is enabled.
		"""
		conf = self.getControlServerConfig()
		if not conf:
			return

		key = conf["key"]
		insecure = False
		if conf.get("selfHosted", False):
			port = conf.get("port", 6837)
			hostname = "localhost"
			insecure = True
			self.startLocalServer(port, key)
		else:
			try:
				hostname, port = addressToHostPort(conf["host"])
			except ValueError:
				log.error("Invalid host in auto-connect config, cannot connect.")
				return

		mode = RemoteConnectionMode(conf["connectionMode"]).toConnectionMode()
		info = ConnectionInfo(mode=mode, hostname=hostname, port=port, key=key, insecure=insecure)
		self.connect(info)

	def getSwapTargetInfo(self) -> ConnectionInfo | None:
		"""Get target info for swapping between leader and follower modes.

		:return: Target connection info for the swap, or None if no active session or swap is not possible.
		"""
		client = self.getClient()
		if not client:
			return None

		currentInfo = None
		newMode = None

		if client.leaderSession:
			currentInfo = self.getCurrentConnectionInfo()
			newMode = ConnectionMode.FOLLOWER
		elif client.followerSession:
			currentInfo = self.getCurrentConnectionInfo()
			newMode = ConnectionMode.LEADER

		if currentInfo and newMode:
			targetInfo = ConnectionInfo(
				hostname=currentInfo.hostname,
				port=currentInfo.port,
				key=currentInfo.key,
				mode=newMode,
				insecure=currentInfo.insecure,
			)
			return targetInfo

		return None

	def shouldConfirmDisconnectAsFollower(self) -> bool:
		"""Check if the user has enabled confirmation for disconnecting as follower."""
		conf = _remoteClient.configuration.getRemoteConfig().get("ui", {})
		return conf.get("confirmDisconnectAsFollower", True)

	def isAutoConnectEnabled(self) -> bool:
		"""Check if auto-connect is currently enabled in config."""
		conf = self.getControlServerConfig()
		return conf.get("autoconnect", False) if conf else False

	def setAsAutoConnect(self, conn: dict[str, Any]) -> None:
		"""Set a connection as the auto-connect configuration.

		:param conn: Connection dictionary from the connection manager.
		"""
		controlServer = config.conf["remote"]["controlServer"]
		controlServer["autoconnect"] = True
		controlServer["selfHosted"] = conn.get("selfHosted", False)
		controlServer["connectionMode"] = 1 if conn["mode"] == "leader" else 0
		controlServer["key"] = conn["key"]

		if conn.get("selfHosted", False):
			controlServer["port"] = conn["port"]
		else:
			# Format host with port if non-default
			host = conn["host"]
			if conn["port"] != 6837:
				host = f"{host}:{conn['port']}"
			controlServer["host"] = host
