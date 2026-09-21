"""Check preference persistence and Remote negotiation without NVDA or audio hardware."""

import importlib.util
from collections import defaultdict
from enum import StrEnum
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_audio_service import audioModule, runtimeWithEvents


class FakeAction:
	def __init__(self):
		self.handlers = set()

	def register(self, callback):
		self.handlers.add(callback)

	def unregister(self, callback):
		self.handlers.discard(callback)

	def notify(self, **payload):
		for callback in tuple(self.handlers):
			callback(**payload)


class FakeConfig(dict):
	def getConfigValidation(self, path):
		return SimpleNamespace(default="default")


def loadService():
	path = Path(__file__).parents[1] / "addon/globalPlugins/remotePlusPlus/service.py"
	spec = importlib.util.spec_from_file_location("remote_test.service", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		"sys.modules",
		{
			"remote_test": SimpleNamespace(),
			"remote_test.audio": audioModule,
			"addonHandler": SimpleNamespace(initTranslation=lambda: None),
			"logHandler": SimpleNamespace(log=logging.getLogger(__name__)),
			"globalVars": SimpleNamespace(appArgs=SimpleNamespace(configPath="unused")),
			"queueHandler": SimpleNamespace(
				eventQueue=object(),
				queueFunction=lambda queue, function, *args, **kwargs: function(*args, **kwargs),
			),
			"config": SimpleNamespace(conf=FakeConfig(audio={"outputDevice": "default"})),
			"extensionPoints": SimpleNamespace(
				callWithSupportedKwargs=lambda handler, **payload: handler(**payload),
			),
			"synthDriverHandler": SimpleNamespace(
				synthChanged=FakeAction(),
				getSynth=Mock(return_value=SimpleNamespace(name="espeak", isSupported=lambda setting: False)),
			),
			"config.configFlags": SimpleNamespace(RemoteConnectionMode=Mock()),
			"_remoteClient": SimpleNamespace(remoteRunning=lambda: False),
			"_remoteClient.connectionInfo": SimpleNamespace(
				ConnectionInfo=Mock(),
				ConnectionMode=StrEnum("ConnectionMode", {"LEADER": "master", "FOLLOWER": "slave"}),
			),
			"_remoteClient.protocol": SimpleNamespace(
				RemoteMessageType=SimpleNamespace(
					ERROR="error",
					CLIENT_LEFT="client_left",
					CLIENT_JOINED="client_joined",
					CHANNEL_JOINED="channel_joined",
					SPEAK="speak",
					CANCEL="cancel",
					PAUSE_SPEECH="pause_speech",
					TONE="tone",
					WAVE="wave",
					DISPLAY="display",
				),
				addressToHostPort=Mock(),
			),
		},
	):
		module.__dict__["_"] = lambda text: text
		spec.loader.exec_module(module)
	return module


serviceModule = loadService()
AudioSettings = audioModule.AudioSettings


def makeEnvelope(kind, **fields):
	return audioModule.make_audio_envelope(kind, **(AudioSettings().formatFields() | fields))


class FakeAudio:
	def __init__(self, callback):
		self.callback = callback
		self.state = "off"
		self.error = None
		self.errorCode = None
		self.role = None
		self.sources = 0
		self.isReceiving = True
		self.settings = AudioSettings()
		self.starts = []
		self.events = []
		self.generation = 0
		self.muted = False

	def setMuted(self, muted):
		self.muted = muted

	def stop(self):
		self.generation += 1
		self.state = "off"
		self.sources = 0
		self.role = None
		self.notify_state("off")

	def terminate(self):
		self.stop()

	def start(self, host, mode, key, *, sources, settings, **kwargs):
		self.generation += 1
		self.state = "on"
		self.role = "publisher" if mode == "slave" else "subscriber"
		self.settings = settings
		self.sources = sources
		self.starts.append((host, mode, key, sources, settings))
		self.notify_state("on")
		return True

	def is_active(self):
		return self.state in {"starting", "on"}

	def notify_state(self, state, error=None):
		self.events.append((state, error))
		self.callback(audioModule.AudioStateEvent(state, error, self.generation))


class AudioNegotiationTests(unittest.TestCase):
	def setUp(self):
		serviceModule.config.conf["audio"]["outputDevice"] = "default"
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		with patch.object(serviceModule.globalVars.appArgs, "configPath", self.directory.name):
			self.service = serviceModule.RemoteService()
		self.service.audio = FakeAudio(self.service._onNativeAudioState)
		self.info = SimpleNamespace(mode="master", hostname="remote.example", key="room")
		self.service.getCurrentConnectionInfo = Mock(return_value=self.info)
		self.service.getClient = Mock(
			return_value=SimpleNamespace(
				followerSession=SimpleNamespace(leaders={7: {}, 8: {}}),
				leaderSession=SimpleNamespace(followers={7}),
			),
		)
		self.transport = Mock(connected=True, successfulConnects=1, _remotePlusPlusStaleFollowers=None)
		self.transport.inboundHandlers = defaultdict(FakeAction)
		self.transport.registerInbound.side_effect = lambda kind, handler: self.transport.inboundHandlers[
			kind
		].register(handler)
		self.transport.unregisterInbound.side_effect = lambda kind, handler: self.transport.inboundHandlers[
			kind
		].unregister(handler)
		self.transport.transportDisconnected = FakeAction()
		self.localMachine = Mock(isMuted=False)
		client = self.service.getClient.return_value
		client.localMachine = self.localMachine
		client._doToggleMute = lambda: setattr(self.localMachine, "isMuted", not self.localMachine.isMuted)
		self.service.getClient.return_value.leaderSession.localMachine = self.localMachine
		for kind, handler in (
			("speak", self.localMachine.speak),
			("cancel", self.localMachine.cancelSpeech),
			("pause_speech", self.localMachine.pauseSpeech),
			("tone", self.localMachine.beep),
			("wave", self.localMachine.playWave),
			("display", self.localMachine.display),
		):
			self.transport.registerInbound(kind, handler)
		self.service.getClient.return_value.leaderSession.transport = self.transport
		self.service.getClient.return_value.followerSession.transport = self.transport
		self.sent = self.transport.send
		self.service._registerAudioTransport(self.transport)
		self.addCleanup(self.service.terminate)

	def flush(self):
		self.service._audioWorker.submit(lambda: None).result(timeout=2)

	def request(self, sources=1):
		self.assertTrue(self.service.requestAudioSources(sources))
		self.flush()
		return next(iter(self.service._pendingAudioRequests))

	def respond(self, requestId, sources=1, **fields):
		envelope = makeEnvelope(
			"response",
			request_id=requestId,
			sources=sources,
			status="ok",
			**fields,
		)
		self.service._handleAudioResponse(envelope, 7, self.service._audioEpoch)

	def testPreferencesPersistWithoutStartingAudioAndRollbackOnSaveFailure(self):
		manager = self.service.connection_manager
		settings = AudioSettings(80, 64, 1, 20)
		self.assertTrue(manager.setAudioSettings(settings))
		self.service.applyAudioSettings()
		self.flush()
		self.assertFalse(self.service.audio.starts)
		self.sent.assert_not_called()
		manager.data = manager._getDefaultData()
		manager.loadConfig()
		self.assertEqual(manager.getAudioSettings(), settings)
		with patch.object(serviceModule.os, "replace", side_effect=OSError("read-only")):
			with self.assertLogs(level="ERROR"):
				self.assertFalse(manager.setAudioSettings(AudioSettings()))
		self.assertEqual(manager.getAudioSettings(), settings)

	def testMissingNegotiatedFormatCancelsWithoutOverwritingPreference(self):
		settings = AudioSettings(20, 64, 1, 20)
		self.service.connection_manager.setAudioSettings(settings)
		requestId = self.request()
		response = makeEnvelope("response", request_id=requestId, sources=1, status="ok")
		del response["codec"]
		self.service._handleAudioResponse(response, 7, self.service._audioEpoch)
		self.flush()
		self.assertFalse(self.service.audio.starts)
		self.assertEqual(self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]["sources"], 0)
		self.assertEqual(self.service.connection_manager.getAudioSettings(), settings)

	def testLatestApplyDuringNegotiationPreservesSources(self):
		requestId = self.request(3)
		self.service.connection_manager.setAudioSettings(AudioSettings(10, 96, 1))
		self.service.applyAudioSettings()
		self.service.connection_manager.setAudioSettings(AudioSettings(80, 64, 1, 20))
		self.service.applyAudioSettings()
		self.respond(requestId, 3)
		self.flush()
		latestId = next(iter(self.service._pendingAudioRequests))
		self.assertNotEqual(latestId, requestId)
		self.respond(latestId, 3, bitrate_kbps=64, channels=1, frame_ms=20)
		self.assertEqual(self.service.audio.settings, AudioSettings(80, 64, 1, 20))
		self.assertEqual(self.service.getAudioSources(), 3)
		self.assertFalse(self.service.isAudioRequestPending())

	def testStopAndDisconnectNeverReopenAudioAfterApplyOrLateResponse(self):
		requestId = self.request(0)
		self.service.applyAudioSettings()
		self.respond(requestId, 0)
		self.flush()
		self.assertFalse(self.service.audio.starts)
		requestId = self.request()
		self.service.stopAudio()
		self.flush()
		self.respond(requestId)
		self.assertFalse(self.service.audio.starts)
		self.assertEqual(self.service.getAudioSources(), 0)

	def testStopCannotSendCancellationBeforeQueuedAudioRequest(self):
		with patch.object(self.service, "_queueAudioTask"):
			self.assertTrue(self.service.requestAudioSources(1))
		requestId = next(iter(self.service._pendingAudioRequests))
		settings = self.service.connection_manager.getAudioSettings()
		epoch = self.service._audioEpoch
		entered, release = threading.Event(), threading.Event()
		sent = []

		def send(envelope):
			sent.append(envelope)
			entered.set()
			release.wait(1)
			return True

		with (
			patch.object(self.service.audio, "stop"),
			patch.object(self.service, "_sendAudioMessage", side_effect=send),
		):
			request = threading.Thread(
				target=self.service._sendAudioRequest,
				args=(requestId, 1, settings, epoch),
			)
			request.start()
			self.assertTrue(entered.wait(1))
			stop = threading.Thread(target=self.service.stopAudio)
			stop.start()
			# The request sender owns the lifecycle lock while its message is sent.
			self.assertTrue(stop.is_alive())
			release.set()
			request.join(1)
			stop.join(1)
		self.assertFalse(request.is_alive())
		self.assertFalse(stop.is_alive())
		self.assertEqual([envelope.get("sources") for envelope in sent], [1, 0])

	def testWorkerSpeechFilterChangesAreQueuedAndStaleChangesIgnored(self):
		queued = []
		with patch.object(
			serviceModule.queueHandler,
			"queueFunction",
			side_effect=lambda queue, function, *args, **kwargs: queued.append((function, args, kwargs)),
		):
			worker = threading.Thread(target=self.service._setRemoteSpeechSuppressed, args=(True,))
			worker.start()
			worker.join(1)
		self.assertFalse(worker.is_alive())
		self.assertEqual(len(queued), 1)
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		function, args, kwargs = queued.pop()
		self.assertFalse(kwargs)
		function(*args)
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		self.assertIsNot(
			self.localMachine.speak,
			next(iter(self.transport.inboundHandlers["speak"].handlers)),
		)

		with patch.object(
			serviceModule.queueHandler,
			"queueFunction",
			side_effect=lambda queue, function, *args, **kwargs: queued.append((function, args, kwargs)),
		):
			for suppressed in (False, True):
				worker = threading.Thread(target=self.service._setRemoteSpeechSuppressed, args=(suppressed,))
				worker.start()
				worker.join(1)
			self.assertEqual(len(queued), 2)
			# Applying the stale enable after the newer disable must do nothing.
			function, args, kwargs = queued.pop(0)
			function(*args)
			function, args, kwargs = queued.pop(0)
			function(*args)
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)

	def testMismatchedResponsesNeverStartPlayback(self):
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		for fields in [
			{"bitrate_kbps": 192},
			{"sources": 2},
			{"channels": 1},
			{"frame_ms": 20},
			{"codec": "pcm"},
		]:
			requestId = self.request()
			self.respond(requestId, **fields)
			self.flush()
			self.assertFalse(self.service.audio.starts)
			self.assertEqual(callback.call_args.args[0].state, "error")
			self.assertEqual(self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]["sources"], 0)

	def testTimeoutIgnoresLateResponse(self):
		requestId = self.request()
		self.sent.reset_mock()
		self.service._audioRequestTimedOut(requestId)
		self.sent.assert_called_once()
		cancel = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
		self.assertEqual(
			cancel["sources"],
			0,
			"timed-out publishers must be released before forgetting the request",
		)
		self.respond(requestId)
		self.assertFalse(self.service.audio.starts)
		self.assertFalse(self.service.isAudioRequestPending())

	def testSystemAudioSuppressesRemoteReplayAndRestoresItWithoutChangingMute(self):
		requestId = self.request(1)
		self.transport.inboundHandlers["speak"].notify()
		self.localMachine.speak.assert_called_once()
		self.localMachine.reset_mock()
		self.respond(requestId, includes_nvda_speech=True)
		for kind in ("speak", "cancel", "pause_speech", "tone", "wave", "display"):
			self.transport.inboundHandlers[kind].notify(origin=7)
		self.localMachine.speak.assert_not_called()
		self.localMachine.beep.assert_not_called()
		self.localMachine.playWave.assert_not_called()
		self.localMachine.cancelSpeech.assert_called_once()
		self.localMachine.pauseSpeech.assert_called_once()
		self.localMachine.display.assert_called_once()
		self.assertFalse(self.localMachine.isMuted)
		# A manual mute change while listening must survive audio reconfiguration.
		self.localMachine.isMuted = True
		self.respond(self.request(2), 2)
		self.assertTrue(self.localMachine.isMuted)
		self.transport.inboundHandlers["speak"].notify()
		self.localMachine.speak.assert_called_once()
		self.respond(self.request(3), 3, includes_nvda_speech=True)
		self.service.audio.state = "error"
		self.service.audio.notify_state("error", "playback failed")
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		self.respond(self.request(1), includes_nvda_speech=True)
		self.service.stopAudio()
		self.flush()
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		self.assertTrue(self.localMachine.isMuted)

	def testMultipleFollowersCannotRaceToPublishDifferentFormats(self):
		self.service.getClient.return_value.leaderSession.followers = {7, 8}
		self.transport.inboundHandlers["client_joined"].notify(client={"id": 8, "connection_type": "slave"})
		self.assertFalse(self.service.requestAudioSources(1))
		self.assertFalse(self.service._pendingAudioRequests)
		self.assertFalse(self.service.audio.starts)

	def testNotReceivedMediaKeepsRemoteSpeechAfterReady(self):
		self.service.audio = audioModule.AudioService()
		self.service.audio.set_state_callback(self.service._onNativeAudioState)
		process = runtimeWithEvents()
		requestId = self.request()
		with (
			patch.object(audioModule, "createAudioRuntime", return_value=process),
			patch.object(audioModule, "Thread"),
		):
			self.respond(requestId, includes_nvda_speech=True)
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		observed = []

		def events(callback):
			for event in (
				{"type": "ready"},
				{"type": "media", "receiving": True},
				{"type": "media", "receiving": False},
				{"type": "media", "receiving": "true"},
				{"type": "media", "receiving": True},
			):
				callback(event)
				self.localMachine.speak.reset_mock()
				self.transport.inboundHandlers["speak"].notify(origin=7, sequence=["feedback"])
				observed.append((self.service.audio.isReceiving, self.localMachine.speak.called))
			self.service.stopAudio()
			self.flush()
			# A late event from the stopped process must not reactivate its media state.
			callback({"type": "media", "receiving": True})
			observed.append((self.service.audio.isReceiving, self.service.audio.state))

		process.run = events
		self.service.audio._run(process, self.service.audio._generation)
		self.assertEqual(
			observed,
			[(False, True), (True, False), (False, True), (False, True), (True, False), (False, "off")],
		)
		self.assertEqual(
			[call.args[0][:2] for call in callback.call_args_list],
			[("on", None), ("off", None)],
		)
		self.assertEqual(self.transport.inboundHandlers["speak"].handlers, {self.localMachine.speak})

	def testSilenceSynthDoesNotClaimSpeechCoverage(self):
		self.info.mode = "slave"
		with patch.object(
			serviceModule.synthDriverHandler,
			"getSynth",
			return_value=SimpleNamespace(name="silence"),
		):
			request = makeEnvelope("request", request_id="silent", sources=1)
			self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
		self.assertIs(response["includes_nvda_speech"], False)

	def testZeroVolumeKeepsSpeechAndEachUtteranceUsesCurrentVolume(self):
		self.info.mode = "slave"
		synth = SimpleNamespace(name="espeak", volume=0, isSupported=lambda setting: setting == "volume")
		with patch.object(serviceModule.synthDriverHandler, "getSynth", return_value=synth):
			self.assertFalse(self.service._publisherIncludesSpeech(1))
			request = makeEnvelope("request", request_id="volume", sources=1)
			self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
			send = self.transport.send
			for volume in (50, 0, 75):
				synth.volume = volume
				send("speak", sequence=["feedback"])
				self.assertEqual(
					self.service._originalAudioSend.call_args.kwargs["remotePlusPlus_speechInAudio"],
					volume > 0,
				)
			# The base driver reports volume=0 even for drivers with no volume setting.
			synth.isSupported = lambda setting: False
			synth.volume = 0
			self.assertTrue(self.service._publisherIncludesSpeech(1))

	def testPublisherReadsSynthOnlyOnMainThreadDuringNegotiationAndChanges(self):
		self.info.mode = "slave"
		reads = []
		volume = 60

		class Synth:
			name = "oneCore"

			def isSupported(self, setting):
				return setting == "volume"

			@property
			def volume(self):
				reads.append(threading.current_thread())
				return volume

		with patch.object(serviceModule.synthDriverHandler, "getSynth", return_value=Synth()):
			request = makeEnvelope("request", request_id="mainThread", sources=1)
			releaseWorker = threading.Event()
			self.service._audioWorker.submit(releaseWorker.wait, 2)
			try:
				self.transport.inboundHandlers["error"].notify(
					origin=7,
					**{audioModule.AUDIO_ENVELOPE_KEY: request},
				)
				# A synth/profile switch can happen before the worker begins startup.
				volume = 0
				serviceModule.synthDriverHandler.synthChanged.notify()
			finally:
				releaseWorker.set()
			self.flush()
			response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
			self.assertIs(response["includes_nvda_speech"], False)
			for volume in (75, 0, 60):
				serviceModule.synthDriverHandler.synthChanged.notify()
				self.flush()
				response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
				self.assertEqual(response["includes_nvda_speech"], volume > 0)
			self.assertTrue(reads)
			self.assertEqual(set(reads), {threading.main_thread()})
			# Even a late/background send must not enter a possibly terminated synth.
			reads.clear()
			self.service._audioWorker.submit(self.transport.send, "speak", sequence=["feedback"]).result(2)
			self.assertFalse(reads)

	def testSecondFollowerReleasesBothSourcesIncludingPendingRequests(self):
		for active in (False, True):
			self.service._audioFollowers = {7}
			requestId = self.request(3)
			if active:
				self.respond(requestId, 3, includes_nvda_speech=True)
			self.sent.reset_mock()
			self.transport.inboundHandlers["client_joined"].notify(
				client={"id": 8, "connection_type": "slave"},
			)
			self.flush()
			self.assertEqual(self.service.getAudioSources(), 0)
			self.assertEqual(self.service.audio.state, "off")
			cancel = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
			self.assertEqual(cancel["sources"], 0)
			self.respond(requestId, 3)
			self.assertEqual(self.service.audio.state, "off")

	def testFallbackSpeechCanStillBePausedAndCancelledAfterMediaRecovers(self):
		self.respond(self.request(), includes_nvda_speech=True)
		self.service.audio.isReceiving = False
		self.transport.inboundHandlers["speak"].notify(origin=7, sequence=["long fallback speech"])
		self.localMachine.speak.assert_called_once()
		self.service.audio.isReceiving = True
		self.transport.inboundHandlers["pause_speech"].notify(origin=7, switch=True)
		self.transport.inboundHandlers["cancel"].notify(origin=7)
		self.localMachine.pauseSpeech.assert_called_once_with(origin=7, switch=True)
		self.localMachine.cancelSpeech.assert_called_once()

	def testSpeechVolumeMetadataOverridesHandshakeWithoutReachingSynth(self):
		self.respond(self.request(), includes_nvda_speech=True)
		for coverage, forwarded in ((False, True), (True, False), ("true", True)):
			self.localMachine.speak.reset_mock()
			self.transport.inboundHandlers["speak"].notify(
				origin=7,
				sequence=["feedback"],
				remotePlusPlus_speechInAudio=coverage,
			)
			if forwarded:
				self.localMachine.speak.assert_called_once_with(sequence=["feedback"])
			else:
				self.localMachine.speak.assert_not_called()
		self.respond(self.request(), includes_nvda_speech=False)
		self.localMachine.speak.reset_mock()
		self.transport.inboundHandlers["speak"].notify(
			origin=7,
			sequence=["audible again"],
			remotePlusPlus_speechInAudio=True,
		)
		self.localMachine.speak.assert_not_called()

	def testRemoteMuteAndReconfigurationShareTheCoreMuteState(self):
		client = self.service.getClient.return_value
		original = self.service._originalToggleMute
		client._doToggleMute()
		self.respond(self.request(3), 3)
		self.assertTrue(self.service.audio.muted)
		for muted in (False, True, False):
			client._doToggleMute()
			self.assertEqual(self.localMachine.isMuted, muted)
			self.assertEqual(self.service.audio.muted, muted)
		self.assertEqual(len(self.service.audio.starts), 1)
		client._doToggleMute()
		self.service.applyAudioSettings()
		self.flush()
		self.respond(next(iter(self.service._pendingAudioRequests)), 3)
		self.assertTrue(self.service.audio.muted)
		self.service.handleRemoteConnectionChanged(False)
		self.flush()
		self.assertIs(client._doToggleMute, original)
		self.assertTrue(self.localMachine.isMuted)

	def testDelayedNativeErrorCannotCancelNewControllerOrPublisher(self):
		for mode in ("master", "slave"):
			with self.subTest(mode=mode):
				self.service.stopAudio()
				self.flush()
				self.info.mode = mode
				audio = self.service.audio = audioModule.AudioService()
				audio.set_state_callback(self.service._onNativeAudioState)
				audio._generation = 1
				audio._state = "on"
				entered, release = threading.Event(), threading.Event()
				notify = audio._notify

				def delayed(notification):
					if threading.current_thread() is oldMonitor:
						entered.set()
						if not release.wait(3):
							raise TimeoutError("test did not release old callback")
					notify(notification)

				oldMonitor = threading.Thread(target=audio._set_state, args=("error", "old failure", 1))
				with patch.object(audio, "_notify", side_effect=delayed):
					oldMonitor.start()
					try:
						self.assertTrue(entered.wait(2))
						audio.stop()
						audio._generation += 1
						audio._state = "on"
						audio._sources = 3
						audio._role = "subscriber" if mode == "master" else "publisher"
						self.service._remoteAudioSources = 3
						self.service._activeAudioRequestId = "new request"
						self.service._publisherOwner = 7 if mode == "slave" else None
						self.sent.reset_mock()
						callback = Mock()
						self.service.setAudioStateCallback(callback)
					finally:
						release.set()
						oldMonitor.join(2)
				self.flush()
				self.assertFalse(oldMonitor.is_alive())
				self.assertEqual(self.service._activeAudioRequestId, "new request")
				self.assertEqual(audio.state, "on")
				self.sent.assert_not_called()
				callback.assert_not_called()

	def testNewlyJoinedFollowerKeepsSpeechAndTones(self):
		self.respond(self.request(), includes_nvda_speech=True)
		self.transport.inboundHandlers["client_joined"].notify(client={"id": 8, "connection_type": "slave"})
		self.flush()
		for kind, handler, payload in (
			("speak", self.localMachine.speak, {"sequence": ["new follower"]}),
			("tone", self.localMachine.beep, {"hz": 440, "length": 30}),
			("wave", self.localMachine.playWave, {"fileName": "tone.wav"}),
			("cancel", self.localMachine.cancelSpeech, {}),
			("pause_speech", self.localMachine.pauseSpeech, {"switch": True}),
		):
			self.transport.inboundHandlers[kind].notify(origin=7, **payload)
			handler.assert_called_once_with(origin=7, **payload)
			handler.reset_mock()
			self.transport.inboundHandlers[kind].notify(origin=8, **payload)
			handler.assert_called_once_with(origin=8, **payload)
		self.assertEqual(self.service.getAudioSources(), 0)

	def testRejectedSourceChangeKeepsAudioOffAndRemoteSpeechAvailable(self):
		self.respond(self.request(), includes_nvda_speech=True)
		self.transport.inboundHandlers["client_joined"].notify(client={"id": 8, "connection_type": "slave"})
		self.flush()
		self.sent.reset_mock()
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		self.assertFalse(self.service.requestAudioSources(3))
		self.flush()
		self.assertEqual(self.service.getAudioSources(), 0)
		self.assertEqual(self.service.audio.state, "off")
		self.transport.inboundHandlers["speak"].notify(origin=7, sequence=["publisher"])
		self.localMachine.speak.assert_called_once()
		self.sent.assert_not_called()
		callback.assert_called_once_with(
			audioModule.AudioStateEvent(
				"error",
				"Remote audio requires exactly one controlled computer in the channel.",
				self.service.audio.generation,
			),
		)

	def testMissingOrUnconfirmedSpeechCoveragePreservesRemoteReplay(self):
		for fields in ({}, {"includes_nvda_speech": False}, {"includes_nvda_speech": 1}):
			with self.subTest(fields=fields):
				self.respond(self.request(), **fields)
				self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		# A microphone-only response must never suppress system speech.
		self.respond(self.request(2), 2, includes_nvda_speech=True)
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)

	def testPublisherDeviceChangesAndUnloadRestoreControllerSpeech(self):
		with patch.object(serviceModule.globalVars.appArgs, "configPath", self.directory.name):
			publisher = serviceModule.RemoteService()
		self.addCleanup(publisher.terminate)
		publisher.audio = FakeAudio(publisher._onNativeAudioState)
		publisher.getCurrentConnectionInfo = Mock(
			return_value=SimpleNamespace(mode="slave", hostname="remote.example", key="room"),
		)
		publisher.getClient = self.service.getClient
		publisher._registerAudioTransport(self.transport)
		requestId = self.request()
		request = makeEnvelope("request", request_id=requestId, sources=1)
		publisher._handleAudioRequest(request, 7, publisher._audioEpoch)

		def relayResponse():
			response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
			self.service._handleAudioResponse(response, 7, self.service._audioEpoch)

		relayResponse()
		self.transport.inboundHandlers["speak"].notify(origin=7, sequence=["publisher"])
		self.localMachine.speak.assert_not_called()
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		for device, synth, includesSpeech in (
			("headset", SimpleNamespace(name="espeak", isSupported=lambda setting: False), False),
			("default", SimpleNamespace(name="espeak", isSupported=lambda setting: False), True),
			("default", SimpleNamespace(name="silence"), False),
			("default", None, False),
			("default", SimpleNamespace(name="espeak", isSupported=lambda setting: False), True),
		):
			serviceModule.config.conf["audio"]["outputDevice"] = device
			with patch.object(serviceModule.synthDriverHandler, "getSynth", return_value=synth):
				serviceModule.synthDriverHandler.synthChanged.notify(audioOutputDevice=device)
				publisher._audioWorker.submit(lambda: None).result(timeout=2)
			relayResponse()
			self.localMachine.speak.reset_mock()
			self.transport.inboundHandlers["speak"].notify(origin=7, sequence=["feedback"])
			self.assertEqual(self.localMachine.speak.called, not includesSpeech)
			self.assertEqual(len(publisher.audio.starts), 1)
			self.assertEqual(len(self.service.audio.starts), 1)
		callback.assert_not_called()
		self.sent.reset_mock()
		publisher.terminate()
		self.sent.assert_called_once()
		self.assertNotIn(
			publisher._handlePublisherSynthChanged,
			serviceModule.synthDriverHandler.synthChanged.handlers,
		)
		relayResponse()
		self.flush()
		self.assertEqual(publisher.audio.state, "off")
		self.assertEqual(self.service.audio.state, "off")
		self.assertEqual(self.service.getAudioSources(), 0)
		self.assertEqual(len(self.transport.inboundHandlers["speak"].handlers), 1)
		self.assertIn(
			("error", "Audio sharing stopped on the controlled computer."),
			[call.args[0][:2] for call in callback.call_args_list],
		)

	def testPublisherExplicitOutputDeviceNeverClaimsSpeechCoverage(self):
		self.info.mode = "slave"
		serviceModule.config.conf["audio"]["outputDevice"] = "headset"
		request = makeEnvelope("request", request_id="headset", sources=1)
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
		self.assertIs(response["includes_nvda_speech"], False)

	def testReconnectSnapshotReplacesStaleCoreFollowers(self):
		self.respond(self.request(), includes_nvda_speech=True)
		self.transport.connected = False
		self.transport.transportDisconnected.notify()
		self.flush()
		self.transport.connected = True
		# Core keeps the old ID when handling a reconnect snapshot.
		self.service.getClient.return_value.leaderSession.followers = {7, 9}
		self.service._registerAudioTransport(self.transport)
		self.transport.inboundHandlers["channel_joined"].notify(
			clients=[{"id": 9, "connection_type": "slave"}, {"id": 10, "connection_type": "master"}],
		)
		self.assertEqual(self.service._audioFollowers, {9})
		requestId = self.request()
		self.respond(requestId)
		self.assertTrue(self.service.isAudioRequestPending(), "stale core member cannot accept a request")
		response = makeEnvelope("response", request_id=requestId, sources=1, status="ok")
		self.service._handleAudioResponse(response, 9, self.service._audioEpoch)
		self.assertEqual(self.service.audio.state, "on")
		self.assertEqual(self.service.getClient.return_value.leaderSession.followers, {7, 9})
		self.transport.inboundHandlers["client_joined"].notify(client={"id": 11, "connection_type": "slave"})
		self.assertFalse(self.service.requestAudioSources(3))
		self.transport.inboundHandlers["client_left"].notify(client={"id": 11})
		self.assertTrue(self.service.requestAudioSources(3))
		self.service.stopAudio()
		self.flush()

	def testReloadKeepsConfirmedFollowersAndAcceptsLaterCoreMembershipChanges(self):
		client = self.service.getClient.return_value
		self.transport.successfulConnects = 2
		client.leaderSession.followers = {7, 9}
		self.transport.inboundHandlers["channel_joined"].notify(
			clients=[{"id": 9, "connection_type": "slave"}],
		)
		self.service.terminate()
		# Core can process membership changes before the new plugin attaches.
		client.leaderSession.followers = {7, 11}
		reloadedModule = loadService()
		with patch.object(reloadedModule.globalVars.appArgs, "configPath", self.directory.name):
			self.service = reloadedModule.RemoteService()
		self.addCleanup(self.service.terminate)
		self.service.audio = FakeAudio(self.service._onNativeAudioState)
		self.service.getCurrentConnectionInfo = Mock(return_value=self.info)
		self.service.getClient = Mock(return_value=client)
		self.service._registerAudioTransport(self.transport)
		self.assertEqual(self.service._audioFollowers, {11})
		self.assertEqual(client.leaderSession.followers, {7, 11})
		requestId = self.request()
		response = makeEnvelope("response", request_id=requestId, sources=1, status="ok")
		self.service._handleAudioResponse(response, 11, self.service._audioEpoch)
		self.assertEqual(self.service.audio.state, "on")
		self.assertEqual(len(self.transport.transportDisconnected.handlers), 1)
		self.assertEqual(len(self.transport.inboundHandlers["channel_joined"].handlers), 1)
		self.service.handleRemoteConnectionChanged(False)
		self.flush()
		# A new connection may reuse IDs: the previous connection's exclusions expire.
		self.transport.successfulConnects += 1
		client.leaderSession.followers = {7}
		self.service._registerAudioTransport(self.transport)
		self.assertEqual(self.service._audioFollowers, {7})

	def testResponseFromAnotherControllerIsIgnored(self):
		requestId = self.request()
		envelope = makeEnvelope("response", request_id=requestId, sources=1, status="ok")
		self.service._handleAudioResponse(envelope, 8, self.service._audioEpoch)
		self.assertTrue(self.service.isAudioRequestPending())
		self.assertFalse(self.service.audio.starts)

	def testPeerDisconnectDuringPublisherStartupCannotResurrectAudio(self):
		self.info.mode = "slave"
		self.service.audio.start = Mock(return_value=True)

		def disconnectWhileStarting(*args, **kwargs):
			self.service.audio.state = "starting"
			return True

		self.service.audio.start.side_effect = disconnectWhileStarting

		def disconnected(timeout):
			self.service.stopAudio()
			return False

		self.service.audio.wait_until_ready = disconnected
		request = makeEnvelope("request", request_id="start", sources=1)
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		self.flush()
		self.assertEqual(self.service.audio.state, "off")
		self.sent.assert_called_once()
		self.assertEqual(
			self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]["error_code"],
			"publisher_stopped",
		)

	def testPublisherHonorsControllerQualityAndProtectsOwner(self):
		self.info.mode = "slave"
		self.service.connection_manager.setAudioSettings(AudioSettings(80, 96, 1))
		request = makeEnvelope(
			"request",
			request_id="first",
			sources=3,
			bitrate_kbps=64,
			channels=1,
			frame_ms=20,
		)
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		self.assertEqual(self.service.audio.settings, AudioSettings(0, 64, 1, 20))
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		self.assertEqual(len(self.service.audio.starts), 1, "unchanged source/quality must reuse publisher")
		self.service._handleAudioRequest(dict(request, channels=2), 8, self.service._audioEpoch)
		self.assertEqual(len(self.service.audio.starts), 1)
		self.assertEqual(
			self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]["status"],
			"error",
		)
		self.service._handleAudioPeerLeft(client={"id": 7})
		self.flush()
		self.assertEqual(self.service.audio.state, "off")

	def testLegacyControllerAndInvalidFormatsNeverStartCapture(self):
		self.info.mode = "slave"
		request = makeEnvelope("request", request_id="legacy", sources=1)
		for fields in (
			{"version": 1},
			{"bitrate_kbps": 7},
			{"bitrate_kbps": True},
			{"channels": 3},
			{"channels": True},
			{"frame_ms": 5},
			{"codec": "pcm"},
			{"codec": None},
		):
			with self.subTest(fields=fields):
				self.service._handleAudioMessage(
					origin=7,
					**{audioModule.AUDIO_ENVELOPE_KEY: request | fields},
				)
				self.flush()
				self.assertFalse(self.service.audio.starts)
				response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
				self.assertEqual(response["status"], "error")
				self.assertEqual(response["version"], fields.get("version", 2))

	def testCancellationDoesNotRequireCodecSettings(self):
		self.info.mode = "slave"
		request = makeEnvelope("request", request_id="active", sources=3)
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		self.assertEqual(self.service.audio.state, "on")
		cancel = audioModule.make_audio_envelope("request", request_id="cancel", sources=0, codec=[])
		self.service._handleAudioRequest(cancel, 7, self.service._audioEpoch)
		self.assertEqual(self.service.audio.state, "off")
		self.assertIsNone(self.service._publisherOwner)

	def testTransportDisconnectStopsActiveAndPendingAudioAndAllowsManualRestart(self):
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		for active in (False, True):
			with self.subTest(active=active):
				requestId = self.request(3)
				if active:
					self.respond(requestId, 3)
				callback.reset_mock()
				self.transport.connected = False
				self.transport.transportDisconnected.notify()
				self.flush()
				self.assertEqual(self.service.audio.state, "off")
				self.assertEqual(self.service.getAudioSources(), 0)
				self.assertFalse(self.service.isAudioRequestPending())
				self.assertFalse(self.service._pendingAudioRequests)
				callback.assert_called_once_with(
					audioModule.AudioStateEvent("off", None, self.service.audio.generation),
				)
				self.transport.connected = True
				self.service._registerAudioTransport(self.transport)
				self.respond(requestId, 3)
				self.assertEqual(self.service.audio.state, "off")
				self.assertEqual(len(self.transport.transportDisconnected.handlers), 1)
				self.transport.inboundHandlers["channel_joined"].notify(
					clients=[{"id": 7, "connection_type": "slave"}],
				)
		requestId = self.request()
		self.respond(requestId)
		self.assertEqual(self.service.audio.state, "on")

	def testReplacedTransportCannotStopCurrentAudio(self):
		oldCallback = next(iter(self.transport.transportDisconnected.handlers))
		transport = Mock(
			connected=True,
			transportDisconnected=FakeAction(),
			successfulConnects=1,
			_remotePlusPlusStaleFollowers=None,
		)
		self.service.getClient.return_value.leaderSession.transport = transport
		self.service._registerAudioTransport(transport)
		self.assertFalse(self.transport.transportDisconnected.handlers)
		self.flush()
		self.respond(self.request())
		oldCallback()
		self.flush()
		self.assertEqual(self.service.audio.state, "on")
		self.service.handleRemoteConnectionChanged(False)
		self.assertFalse(transport.transportDisconnected.handlers)

	def testRuntimeErrorMustMatchActiveRequestAndPeerAndIsReportedOnce(self):
		oldId = self.request()
		self.respond(oldId)
		requestId = self.request(3)
		fault = makeEnvelope(
			"response",
			request_id=oldId,
			sources=0,
			status="error",
			message="device disconnected",
		)
		self.service._handleAudioResponse(fault, 7, self.service._audioEpoch)
		self.assertTrue(self.service.isAudioRequestPending())
		self.respond(requestId, 3)
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		for origin, responseId in ((7, oldId), (8, requestId)):
			self.service._handleAudioResponse(
				dict(fault, request_id=responseId),
				origin,
				self.service._audioEpoch,
			)
		self.assertEqual(self.service.audio.state, "on")
		fault["request_id"] = requestId
		for _ in range(2):
			self.service._handleAudioResponse(fault, 7, self.service._audioEpoch)
		self.flush()
		self.assertEqual(self.service.getAudioSources(), 0)
		self.assertEqual(self.service.audio.state, "off")
		self.assertEqual(
			[call.args[0][:2] for call in callback.call_args_list if call.args[0].state == "error"],
			[("error", "device disconnected")],
		)

	def testPublisherReportsNativeErrorAndUnexpectedExit(self):
		self.info.mode = "slave"
		for events in (
			(),
			({"type": "error", "code": "audio_device_failed", "message": "microphone disconnected"},),
		):
			with self.subTest(events=events):
				self.service.audio = audioModule.AudioService()
				self.service.audio.set_state_callback(self.service._onNativeAudioState)
				process = runtimeWithEvents(*events)
				request = makeEnvelope("request", request_id="running", sources=3)

				def ready(timeout):
					self.service.audio._set_state("on", None, self.service.audio._generation)
					return True

				with (
					patch.object(audioModule, "createAudioRuntime", return_value=process),
					patch.object(audioModule, "Thread"),
					patch.object(self.service.audio, "wait_until_ready", side_effect=ready),
				):
					self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
				self.sent.reset_mock()
				self.service.audio._run(process, self.service.audio._generation)
				self.flush()
				self.flush()
				self.sent.assert_called_once()
				response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
				self.assertEqual(response["request_id"], "running")
				self.assertEqual(response["status"], "error")
				self.assertEqual(response["sources"], 0)
				self.assertEqual(
					response["message"],
					"The audio device is unavailable or stopped working."
					if events
					else "Audio component stopped unexpectedly.",
				)
				self.assertEqual(response.get("error_code"), "audio_device_failed" if events else None)
				self.assertIsNone(self.service._publisherOwner)
				self.assertIsNone(self.service._activeAudioRequestId)

	def testPublisherStartupFailureUsesOnlyPendingResponse(self):
		self.info.mode = "slave"

		def failedStart(*args, **kwargs):
			self.service.audio.state = "error"
			self.service.audio.error = "capture failed"
			self.service.audio.notify_state("error", "capture failed")
			return False

		self.service.audio.start = failedStart
		request = makeEnvelope("request", request_id="startup", sources=1)
		self.service._queueAudioTask(self.service._handleAudioRequest, request, 7, self.service._audioEpoch)
		self.flush()
		self.flush()
		self.sent.assert_called_once()
		self.assertEqual(
			self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]["status"],
			"error",
		)

	def testRemoteNativeErrorsUseControllerTranslation(self):
		callback = Mock()
		self.service.setAudioStateCallback(callback)
		for active in (False, True):
			requestId = self.request()
			if active:
				self.respond(requestId)
			fault = makeEnvelope(
				"response",
				request_id=requestId,
				sources=0,
				status="error",
				error_code="no_microphone",
				message="message in publisher language",
			)
			with patch.object(audioModule, "_", side_effect=lambda value: "translated: " + value):
				self.service._handleAudioResponse(fault, 7, self.service._audioEpoch)
			self.flush()
			self.assertIn(
				("error", "translated: No default microphone is available."),
				[call.args[0][:2] for call in callback.call_args_list],
			)
			self.assertEqual(callback.call_args.args[0].generation, self.service.audio.generation)

	def testPublisherRestartAndStopDiscardQueuedOldFaults(self):
		self.info.mode = "slave"
		request = makeEnvelope("request", request_id="old", sources=1)
		self.service._handleAudioRequest(request, 7, self.service._audioEpoch)
		epoch = self.service._audioEpoch
		event = audioModule.AudioStateEvent("error", "old failure", self.service.audio.generation)
		self.service._handleAudioRequest(dict(request, request_id="new", sources=3), 7, epoch)
		self.sent.reset_mock()
		self.service._handlePublisherAudioError("old", epoch, event)
		self.service._handlePublisherAudioError("new", epoch, event)
		self.assertEqual(self.service.audio.state, "on")
		self.service.stopAudio()
		self.service._handlePublisherAudioError("new", epoch, event)
		self.flush()
		self.sent.assert_called_once()
		response = self.sent.call_args.kwargs[audioModule.AUDIO_ENVELOPE_KEY]
		self.assertEqual(response["request_id"], "new")
		self.assertEqual(response["error_code"], "publisher_stopped")


if __name__ == "__main__":
	unittest.main()
