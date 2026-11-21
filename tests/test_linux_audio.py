"""Linux companion validation; no desktop or connection to a user's channel."""

import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def loadCompanion():
	name = "linux_audio_test"
	path = ROOT / "extras/lindaRemoteAudio/globalPlugins/lindaRemoteAudio/__init__.py"
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	plugin = ModuleType("globalPluginHandler")
	plugin.GlobalPlugin = type("GlobalPlugin", (), {"terminate": lambda self: None})
	logger = ModuleType("logHandler")
	logger.log = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)
	protocol = ModuleType("_remoteClient.protocol")
	protocol.RemoteMessageType = SimpleNamespace(ERROR="error", CLIENT_LEFT="client_left")
	client = ModuleType("_remoteClient")
	with patch.dict(
		sys.modules,
		{
			name: module,
			"globalPluginHandler": plugin,
			"logHandler": logger,
			"_remoteClient": client,
			"_remoteClient.protocol": protocol,
		},
	):
		# The package builder places these shared modules beside the companion.
		module.__path__.append(str(ROOT / "addon/globalPlugins/remotePlusPlus"))
		spec.loader.exec_module(module)
	return module


class LinuxAudioTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.audio = loadCompanion()

	def testRejectsBooleanAndUnsupportedAudioSettings(self):
		valid = {"codec": "opus", "channels": 2, "bitrate_kbps": 96, "frame_ms": 10}
		self.assertTrue(self.audio.validSettings(valid))
		for field, value in (("channels", True), ("bitrate_kbps", 65), ("frame_ms", 5)):
			with self.subTest(field=field):
				self.assertFalse(self.audio.validSettings({**valid, field: value}))
		self.assertFalse(self.audio.validSettings(None))

	@unittest.skipUnless(sys.platform == "linux", "requires selectable POSIX pipes")
	def testStopReleasesBlockedCaptureWithoutWaitingForMoreAudio(self):
		import os

		readFd, writeFd = os.pipe()
		self.addCleanup(os.close, readFd)
		self.addCleanup(os.close, writeFd)
		stop = threading.Event()
		result = []
		worker = threading.Thread(target=lambda: result.append(self.audio.readFrame(readFd, 1920, stop)))
		worker.start()
		stop.set()
		worker.join(1)
		self.assertFalse(worker.is_alive())
		self.assertEqual(result, [None])

	@unittest.skipUnless(sys.platform == "linux", "requires selectable POSIX pipes")
	def testCaptureCombinesPartialReadsAndRejectsPrematureEnd(self):
		import os

		readFd, writeFd = os.pipe()
		self.addCleanup(os.close, readFd)
		os.write(writeFd, b"abcd")
		os.close(writeFd)
		self.assertEqual(self.audio.readFrame(readFd, 4, threading.Event()), b"abcd")
		with self.assertRaises(OSError):
			self.audio.readFrame(readFd, 4, threading.Event())

	def testUntrustedControllerCannotStartOrStopCapture(self):
		plugin = self.audio.GlobalPlugin.__new__(self.audio.GlobalPlugin)
		plugin.closed = threading.Event()
		plugin.transport = object()
		plugin.session = SimpleNamespace(leaders={7: {}})
		plugin.requests = []
		plugin.lock = threading.RLock()
		plugin.requestReady = threading.Event()
		request = {
			"version": 3,
			"kind": "request",
			"request_id": "test",
			"system_audio": True,
			"voice_call": False,
		}
		plugin._handle(origin=8, remotePlusPlus_audio=request)
		self.assertEqual(plugin.requests, [])
		plugin._handle(origin=7, remotePlusPlus_audio={**request, "system_audio": "yes"})
		self.assertEqual(plugin.requests, [])
		plugin._handle(origin=7, remotePlusPlus_audio=request)
		self.assertEqual(len(plugin.requests), 1)

	def newPlugin(self):
		plugin = self.audio.GlobalPlugin.__new__(self.audio.GlobalPlugin)
		plugin.closed = threading.Event()
		plugin.lock = threading.RLock()
		plugin.publisher = plugin.owner = None
		plugin.activeRequest = None
		plugin.retired = []
		plugin.responses = []
		plugin.transport = SimpleNamespace(
			connected=True, send=lambda kind, **payload: plugin.responses.append(payload)
		)
		plugin.session = SimpleNamespace(
			leaders={7: {}, 8: {}},
			getConnectionInfo=lambda: SimpleNamespace(hostname="localhost", key="test"),
		)
		return plugin

	def testOtherControllerCannotStopOwnersCapture(self):
		plugin = self.newPlugin()
		stopped = threading.Event()
		plugin.publisher = SimpleNamespace(stop=stopped.set)
		plugin.owner = 7
		plugin._request(
			plugin.transport, 8, {"request_id": "stop", "system_audio": False, "voice_call": False}
		)
		self.assertFalse(stopped.is_set())
		self.assertEqual(plugin.responses[0]["remotePlusPlus_audio"]["status"], "error")
		plugin._peerLeft(client={"id": 7})
		self.assertTrue(stopped.is_set())
		self.assertIsNone(plugin.publisher)

	def testPublisherFailureNotifiesControllerWithActiveRequest(self):
		plugin = self.newPlugin()
		stopped = threading.Event()
		plugin.publisher = SimpleNamespace(error="capture failed", stop=stopped.set)
		plugin.owner = 7
		plugin.activeRequest = (plugin.transport, 7, "active-request")
		plugin._checkPublisher()
		self.assertTrue(stopped.is_set())
		self.assertIsNone(plugin.publisher)
		response = plugin.responses[0]["remotePlusPlus_audio"]
		self.assertEqual(response["request_id"], "active-request")
		self.assertEqual(response["status"], "error")
		self.assertFalse(response["includes_nvda_speech"])

	def testFailedWorkerStartReleasesOwnerAndResponds(self):
		plugin = self.newPlugin()

		class BrokenPublisher:
			def __init__(self, *args):
				pass

			def start(self):
				raise RuntimeError("thread creation failed")

			def stop(self):
				pass

		with patch.object(self.audio, "Publisher", BrokenPublisher):
			plugin._request(
				plugin.transport,
				7,
				{
					"request_id": "start",
					"system_audio": True,
					"voice_call": False,
					"system_audio_settings": {
						"codec": "opus",
						"channels": 2,
						"bitrate_kbps": 96,
						"frame_ms": 10,
					},
				},
			)
		self.assertIsNone(plugin.publisher)
		self.assertIsNone(plugin.owner)
		self.assertEqual(plugin.responses[0]["remotePlusPlus_audio"]["status"], "error")

	def testReplacementWaitsForOldPublisherToClose(self):
		plugin = self.newPlugin()
		stop = threading.Event()
		release = threading.Event()
		closed = threading.Event()

		def oldWorker():
			stop.wait()
			release.wait(1)
			closed.set()

		old = threading.Thread(target=oldWorker)
		old.start()
		self.addCleanup(old.join, 1)
		self.addCleanup(release.set)
		self.addCleanup(stop.set)
		plugin.publisher = SimpleNamespace(stop=stop.set, thread=old)
		plugin.owner = 7
		startedAfterClose = []

		class Replacement:
			def __init__(self, *args):
				self.error = None
				self.ready = threading.Event()
				self.ready.set()
				self.thread = SimpleNamespace(is_alive=lambda: True)

			def start(self):
				startedAfterClose.append(closed.is_set())

		timer = threading.Timer(0.05, release.set)
		timer.start()
		self.addCleanup(timer.join)
		with patch.object(self.audio, "Publisher", Replacement):
			plugin._request(
				plugin.transport,
				7,
				{
					"request_id": "replace",
					"system_audio": True,
					"voice_call": False,
					"system_audio_settings": {
						"codec": "opus",
						"channels": 2,
						"bitrate_kbps": 96,
						"frame_ms": 10,
					},
				},
			)
		self.assertEqual(startedAfterClose, [True])
		self.assertEqual(plugin.responses[0]["remotePlusPlus_audio"]["status"], "ok")

	def testPackageContainsSharedSourcesWithoutWindowsBinaries(self):
		import tempfile
		from zipfile import ZipFile

		spec = importlib.util.spec_from_file_location(
			"build_linda_addon", ROOT / "tools/build_linda_addon.py"
		)
		builder = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(builder)
		with tempfile.TemporaryDirectory() as folder:
			output = Path(folder) / "companion.nvda-addon"
			builder.build(output)
			with ZipFile(output) as archive:
				self.assertIn(b"platforms = linux", archive.read("manifest.ini"))
				self.assertFalse(any(name.endswith(".dll") for name in archive.namelist()))
				for name in ("audioCodec.py", "audioTransport.py"):
					self.assertEqual(
						archive.read(f"globalPlugins/lindaRemoteAudio/{name}"),
						(ROOT / "addon/globalPlugins/remotePlusPlus" / name).read_bytes(),
					)

	@unittest.skipUnless(sys.platform == "linux", "requires Linux system libopus")
	def testSystemCodecRoundTripAllNegotiatedFormats(self):
		for bitrate in (64, 96, 192):
			for channels in (1, 2):
				for duration in (10, 20):
					with self.subTest(bitrate=bitrate, channels=channels, duration=duration):
						encoder = self.audio.OpusCodec(channels, bitrate, duration, encoder=True)
						decoder = self.audio.OpusCodec(channels, bitrate, duration, encoder=False)
						try:
							payload = encoder.encode(bytes(encoder.pcmBytes))
							self.assertEqual(len(payload), bitrate * duration // 8)
							self.assertEqual(
								len(decoder.decode(payload)), 48000 * duration // 1000 * channels * 2
							)
						finally:
							encoder.close()
							decoder.close()
