"""Exercise audio worker event races without loading an NVDA instance."""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def loadAudioService():
	path = Path(__file__).parents[1] / "addon/globalPlugins/remotePlusPlus/audio.py"
	spec = importlib.util.spec_from_file_location("audio_under_test", path)
	module = importlib.util.module_from_spec(spec)
	with patch.dict(
		"sys.modules",
		{
			"addonHandler": SimpleNamespace(initTranslation=lambda: None),
			"logHandler": SimpleNamespace(log=logging.getLogger(__name__)),
			"_remoteClient": SimpleNamespace(),
			"_remoteClient.connectionInfo": SimpleNamespace(ConnectionMode=SimpleNamespace(LEADER="master")),
		},
	):
		module.__dict__["_"] = lambda value: value
		spec.loader.exec_module(module)
	return module


audioModule = loadAudioService()
AudioService = audioModule.AudioService


def runtimeWithEvents(*events):
	return SimpleNamespace(
		run=lambda callback: [callback(event) for event in events],
		stop=Mock(),
		setMuted=Mock(),
	)


class AudioServiceTests(unittest.TestCase):
	def testInvalidPreferencesFallBackIndependently(self):
		for value in [None, [], "invalid", {"bufferMs": True, "quality": []}]:
			self.assertEqual(audioModule.normalizeAudioSettings(value), audioModule.AudioSettings())
		self.assertEqual(
			audioModule.normalizeAudioSettings({"bufferMs": 80, "quality": "unknown"}),
			audioModule.AudioSettings(80),
		)
		self.assertEqual(
			audioModule.normalizeAudioSettings({"bufferMs": -1, "quality": "16000_mono"}),
			audioModule.AudioSettings(channels=1),
		)
		for quality in ("48000_mono", "24000_mono", "16000_mono"):
			self.assertEqual(
				audioModule.normalizeAudioSettings({"bufferMs": 20, "quality": quality}),
				audioModule.AudioSettings(20, channels=1),
			)
		self.assertEqual(
			audioModule.normalizeAudioSettings(
				{"bufferMs": 10, "bitrateKbps": 192, "channels": True, "frameMs": 20},
			),
			audioModule.AudioSettings(10, 192, 2, 20),
		)
		for field in ("bufferMs", "bitrateKbps", "channels", "frameMs"):
			for invalid in (None, [], True, "20", -1):
				self.assertEqual(
					audioModule.normalizeAudioSettings({field: invalid}),
					audioModule.AudioSettings(),
				)

	def testWorkerReceivesSettingsAndIdentity(self):
		service = AudioService()
		runtime = runtimeWithEvents()
		settings = audioModule.AudioSettings(80, 64, 1, 20)
		with (
			patch.object(audioModule, "createAudioRuntime", return_value=runtime) as start,
			patch.object(audioModule, "Thread"),
		):
			self.assertTrue(
				service.start(
					"remote.example", "master", "room", settings=settings, systemDeviceId="speakers"
				),
			)
		start.assert_called_once_with(
			"remote.example",
			6838,
			"room",
			"subscriber",
			1,
			settings,
			False,
			"system_audio",
			None,
		)
		self.assertEqual(service.settings, settings)
		service._event(
			{"type": "capture_device", "device_id": "speakers"}, service.generation, "system_audio"
		)
		self.assertEqual(service.systemCaptureDeviceId, "speakers")
		service.stop()
		self.assertIsNone(service.systemCaptureDeviceId)
		service._event(
			{"type": "capture_device", "device_id": "stale"}, service.generation - 1, "system_audio"
		)
		self.assertIsNone(service.systemCaptureDeviceId)
		runtime.stop.assert_called_once()

	def testSourceMaskAndEnvelopeValidation(self):
		self.assertEqual(audioModule.normalize_source_mask(0), 0)
		self.assertEqual(audioModule.normalize_source_mask(3), 3)
		self.assertIsNone(audioModule.normalize_source_mask(True))
		self.assertIsNone(audioModule.normalize_source_mask(4))
		envelope = audioModule.make_audio_envelope(
			"request",
			request_id="request-1",
			sources=audioModule.AUDIO_SOURCE_SYSTEM,
		)
		self.assertEqual(audioModule.parse_audio_envelope(envelope), envelope)
		self.assertEqual(
			audioModule.parse_audio_envelope(audioModule.make_audio_envelope("device_changed")),
			audioModule.make_audio_envelope("device_changed"),
		)
		for fields in [
			{"kind": []},
			{"version": True},
			{"version": 3.0},
			{"kind": "response", "status": {}},
			{"includes_nvda_speech": "true"},
			{"includes_nvda_speech": 1},
		]:
			self.assertIsNone(audioModule.parse_audio_envelope(dict(envelope, **fields)))
		self.assertIsNone(audioModule.parse_audio_envelope({"type": "error"}))
		self.assertIsNone(
			audioModule.parse_audio_envelope(
				{"version": 1, "kind": "request", "request_id": "", "sources": 1},
			),
		)
		self.assertIsNone(
			audioModule.parse_audio_envelope(
				{"version": audioModule.AUDIO_PROTOCOL_VERSION, "kind": "hello", "sources": 1},
			),
		)

	def testOnlyPublisherStreamsReceiveCaptureDevices(self):
		service = AudioService()
		runtimes = [runtimeWithEvents() for _ in range(3)]
		for runtime, deviceId in zip(runtimes, ("speakers", "microphone", None)):
			runtime.captureDeviceId = deviceId
		with (
			patch.object(audioModule, "createAudioRuntime", side_effect=runtimes) as create,
			patch.object(audioModule, "Thread"),
		):
			self.assertTrue(
				service.start(
					"remote.example",
					"slave",
					"room",
					sources=3,
					systemDeviceId="speakers",
					microphoneDeviceId="microphone",
				),
			)
		self.assertEqual([call.args[-1] for call in create.call_args_list], ["speakers", "microphone", None])
		self.assertEqual(service.captureDeviceIds, ("speakers", "microphone"))
		service.stop()
		self.assertEqual(service.captureDeviceIds, (None, None))

	def testVoiceCallUsesIndependentDirectionalStreams(self):
		service = AudioService()
		runtimes = [runtimeWithEvents(), runtimeWithEvents()]
		settings = audioModule.AudioSettings(20, 64, 1, 20)
		with (
			patch.object(audioModule, "createAudioRuntime", side_effect=runtimes) as create,
			patch.object(audioModule, "Thread"),
		):
			self.assertTrue(
				service.start(
					"remote.example",
					"master",
					"room",
					sources=audioModule.AUDIO_SOURCE_VOICE,
					settings=settings,
					voiceSettings=settings,
					microphoneDeviceId="microphone",
				),
			)
		self.assertEqual(
			[(call.args[3], call.args[4], call.args[7]) for call in create.call_args_list],
			[
				("subscriber", audioModule.AUDIO_SOURCE_VOICE, "voice_controlled_to_controller"),
				("publisher", audioModule.AUDIO_SOURCE_MICROPHONE, "voice_controller_to_controlled"),
			],
		)
		self.assertEqual([call.args[-1] for call in create.call_args_list], [None, "microphone"])
		service.stop()
		for runtime in runtimes:
			runtime.stop.assert_called_once()

	def testEnvelopeNamesSystemAndVoiceSettingsExplicitly(self):
		envelope = audioModule.make_audio_envelope(
			"request",
			request_id="request-1",
			system_audio=True,
			voice_call=True,
			system_audio_settings=audioModule.AudioSettings(10, 96, 2, 10),
			voice_call_settings=audioModule.AudioSettings(20, 64, 1, 20),
		)
		self.assertEqual(audioModule.audio_sources_from_envelope(envelope), 3)
		self.assertEqual(audioModule.parse_audio_envelope(envelope), envelope)
		self.assertIsNone(audioModule.parse_audio_envelope(dict(envelope, voice_call=False)))

	def testMuteBeforeStartupAndLive(self):
		service = AudioService()
		runtime = runtimeWithEvents()
		service.setMuted(True)
		with (
			patch.object(audioModule, "createAudioRuntime", return_value=runtime) as start,
			patch.object(audioModule, "Thread"),
		):
			self.assertTrue(service.start("remote.example", "master", "room"))
		self.assertTrue(start.call_args.args[-3])
		service.setMuted(False)
		runtime.setMuted.assert_called_once_with(False)
		service.stop()

	def testMissingRuntimeReportsError(self):
		service = AudioService()
		with (
			patch.object(audioModule, "createAudioRuntime", side_effect=ImportError("unavailable")),
			self.assertLogs(level="ERROR"),
		):
			self.assertFalse(service.start("audio.example", "master", "key"))
		self.assertEqual(service.state, "error")
		self.assertEqual(service.error, "Audio component failed.")

	def testPartialRuntimeInitializationIsCleanedUp(self):
		service = AudioService()
		first = runtimeWithEvents()
		with (
			patch.object(audioModule, "createAudioRuntime", side_effect=[first, ImportError("unavailable")]),
			self.assertLogs(level="ERROR"),
		):
			self.assertFalse(
				service.start(
					"audio.example",
					"master",
					"key",
					sources=audioModule.AUDIO_SOURCE_VOICE,
				),
			)
		first.stop.assert_called_once()

	def testUnexpectedWorkerFailureStopsOtherStreams(self):
		service = AudioService()
		failed = runtimeWithEvents()
		other = runtimeWithEvents()
		failed.run = Mock(side_effect=RuntimeError("worker failed"))
		service._runtimes = {"failed": failed, "other": other}
		service._generation = 1
		with self.assertLogs(level="ERROR"):
			service._run(failed, 1, "failed")
		other.stop.assert_called_once()

	def testLateReadyCannotReviveFailedSession(self):
		service = AudioService()
		runtime = runtimeWithEvents()
		service._runtimes = {"system_audio": runtime}
		service._generation = 1
		service._state = "starting"
		service._expectedReady = 1
		with self.assertLogs(level="ERROR"):
			service._event(
				{"type": "error", "code": "audio_device_failed", "message": "device stopped"},
				1,
				"system_audio",
			)
		service._event({"type": "ready"}, 1, "system_audio")
		self.assertEqual(service.state, "error")

	def testStaleWorkerFailureCannotStopRestartedStreams(self):
		service = AudioService()
		old = runtimeWithEvents()
		current = runtimeWithEvents()
		service._runtimes = {"current": current}
		service._generation = 2
		old.run = Mock(side_effect=RuntimeError("stale worker failed"))
		with self.assertLogs(level="ERROR"):
			service._run(old, 1, "old")
		current.stop.assert_not_called()

	def testPublisherRejectsEmptySourceMask(self):
		service = AudioService()
		with patch.object(service, "_set_error") as setError:
			self.assertFalse(service.start("audio.example", "follower", "key", sources=0))
		setError.assert_called_once_with("No audio source is enabled.")

	def testOldProcessCannotChangeRestartedSession(self):
		service = AudioService()
		old = runtimeWithEvents({"type": "ready"}, {"type": "error", "message": "old error"})
		service._runtime = old
		service._generation = 1
		service.stop()
		current = object()
		service._runtime = current
		service._generation += 1
		service._state = "starting"
		callback = Mock()
		service.set_state_callback(callback)
		service._run(old, 1)
		self.assertEqual(service.state, "starting")
		self.assertIsNone(service.error)
		self.assertIs(service._runtime, current)
		callback.assert_not_called()

	def testProcessExitPreservesSpecificErrorAndClosesPipes(self):
		service = AudioService()
		process = runtimeWithEvents(
			{"type": "ready"},
			{"type": "error", "code": "audio_device_failed", "message": "capture disconnected"},
		)
		service._runtime = process
		service._generation = 1
		with self.assertLogs(level="ERROR") as logs:
			service._run(process, 1)
		self.assertEqual(service.state, "error")
		self.assertEqual(service.error, "The audio device is unavailable or stopped working.")
		self.assertEqual(service.errorCode, "audio_device_failed")
		self.assertIn("capture disconnected", "\n".join(logs.output))
		self.assertIsNone(service._runtime)

	def testUnknownAndLegacyErrorDetailsAreLoggedButNeverSpoken(self):
		for code in (None, "future_error", [], "udp_registration_failed", "no_microphone"):
			service = AudioService()
			process = runtimeWithEvents(
				{"type": "error", "code": code, "message": "raw driver/network detail"},
			)
			service._runtime = process
			with patch.object(audioModule, "_", side_effect=lambda value: "translated: " + value):
				with self.assertLogs(level="ERROR") as logs:
					service._run(process, 0)
			self.assertTrue(service.error.startswith("translated: "))
			self.assertNotIn("raw driver/network detail", service.error)
			self.assertIn("raw driver/network detail", "\n".join(logs.output))


if __name__ == "__main__":
	unittest.main()
