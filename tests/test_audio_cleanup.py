"""Resource ownership and shutdown regressions without network or device changes."""

from contextlib import contextmanager, nullcontext
import ctypes
import importlib
import sys
import tempfile
import threading
import time
import traceback
import weakref
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_audio_runtime import newRuntime, runtimeModule, transport
from test_audio_service import AudioService, audioModule
from test_audio_negotiation import serviceModule


class CleanupTests(unittest.TestCase):
	def testExternalStopErrorsReleasePlayerBeforeOwnerComExit(self):
		for action in ("stop", "mute", "media gap"):
			with self.subTest(action=action):
				refs, calls = [], []

				class Player:
					def __init__(self, **kwargs):
						refs.append(weakref.ref(self))

					def fail(self):
						raise OSError("native stop failed")

					def stop(self):
						try:
							self.fail()
						except OSError as error:
							raise RuntimeError("stop failed") from error

					def close(self):
						pass

					def enableTrimmingLeadingSilence(self, enable):
						pass

					def __del__(self):
						calls.append(("destroy", threading.get_ident()))

				def uninitialize():
					calls.append(("uninitialize", threading.get_ident()))
					calls.append(("alive", sum(ref() is not None for ref in refs)))

				co = SimpleNamespace(
					COINIT_MULTITHREADED=0,
					CoInitializeEx=lambda flags: None,
					CoUninitialize=uninitialize,
				)
				wave = SimpleNamespace(WavePlayer=Player, AudioPurpose=SimpleNamespace(SPEECH=1))
				runtime = newRuntime()
				with patch.dict(sys.modules, comtypes=co, nvwave=wave):
					worker = threading.Thread(target=runtime._worker, args=(runtime._play,))
					worker.start()
					try:
						with runtime.condition:
							self.assertTrue(runtime.condition.wait_for(lambda: runtime.readyCount, 2))
						try:
							if action == "stop":
								runtime.stop()
							elif action == "mute":
								runtime.setMuted(True)
							else:
								runtime.receiving = True
								runtime._receive(None, bytes(16), lambda event: None)
						except RuntimeError as error:
							# Keep the caller's exception alive while the device owner exits.
							runtime.stopping.set()
							worker.join(2)
							self.assertFalse(worker.is_alive())
							self.assertEqual(
								calls,
								[("destroy", worker.ident), ("uninitialize", worker.ident), ("alive", 0)],
							)
							message = "".join(traceback.format_exception(error))
							self.assertIn("native stop failed", message)
							self.assertIn("RuntimeError: stop failed", message)
						else:
							self.fail("Stop failure was swallowed")
					finally:
						runtime.stopping.set()
						worker.join(2)

	def testPlayerDestroyedOnOwnerThreadBeforeComExitEvenWithChainedErrors(self):
		for failure in (None, "create", "configure", "feed", "close", "feed+close"):
			with self.subTest(failure=failure):
				refs = []
				calls = []

				class Player:
					def __init__(self, **kwargs):
						refs.append(weakref.ref(self))
						if failure == "create":
							raise OSError("create failed")

					def feed(self, payload, onDone=None):
						if failure in ("feed", "feed+close"):
							raise OSError("feed failed")

					def enableTrimmingLeadingSilence(self, enable):
						if failure == "configure":
							raise OSError("configure failed")

					def close(self):
						if failure in ("close", "feed+close"):
							raise OSError("close failed")

					def __del__(self):
						calls.append(("destroy", threading.get_ident()))

				def uninitialize():
					calls.append(("uninitialize", threading.get_ident()))
					calls.append(("alive", sum(ref() is not None for ref in refs)))

				co = SimpleNamespace(
					COINIT_MULTITHREADED=0,
					CoInitializeEx=lambda flags: None,
					CoUninitialize=uninitialize,
				)
				wave = SimpleNamespace(WavePlayer=Player, AudioPurpose=SimpleNamespace(SPEECH=1))
				runtime = newRuntime()
				if failure in ("feed", "feed+close"):
					runtime.buffer.frames.append(b"sample")
				else:
					runtime.stopping.set()
				with patch.dict(sys.modules, comtypes=co, nvwave=wave):
					worker = threading.Thread(target=runtime._worker, args=(runtime._play,))
					worker.start()
					worker.join(2)
				self.assertFalse(worker.is_alive())
				self.assertEqual(
					calls,
					[("destroy", worker.ident), ("uninitialize", worker.ident), ("alive", 0)],
				)
				if failure:
					self.assertIsNotNone(runtime.error)
					self.assertIsNone(runtime.error.__traceback__)
					self.assertIsNone(runtime.error.__context__)
					for stage in failure.split("+"):
						self.assertIn(stage + " failed", str(runtime.error))

	def testDnsCancellationAllowsRemoteTerminationBeforeResolverReturns(self):
		entered, release, terminated = threading.Event(), threading.Event(), threading.Event()
		dnsThreads = []
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		with patch.object(serviceModule.globalVars.appArgs, "configPath", directory.name):
			owner = serviceModule.RemoteService()
		self.addCleanup(owner.terminate)

		def lookup(*args, **kwargs):
			entered.set()
			release.wait(3)
			return [(2, 1, 6, "", ("127.0.0.1", 6838))]

		def thread(**kwargs):
			value = threading.Thread(**kwargs)
			dnsThreads.append(value)
			return value

		def terminate():
			owner.terminate()
			terminated.set()

		with (
			patch.object(transport.socket, "getaddrinfo", side_effect=lookup) as resolver,
			patch.object(transport.socket, "socket") as socket,
			patch.object(transport, "Thread", side_effect=thread),
			patch.object(audioModule, "createAudioRuntime", side_effect=runtimeModule.AudioRuntime),
		):
			self.assertTrue(owner.audio.start("test.example", "master", "test"))
			terminator = threading.Thread(target=terminate, daemon=True)
			try:
				self.assertTrue(entered.wait(1))
				terminator.start()
				self.assertTrue(terminated.wait(1))
				self.assertFalse(release.is_set())
				# Repeated attempts time out without accumulating more stuck resolver threads.
				for _ in range(2):
					with self.assertRaises(TimeoutError):
						transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 0.05)
				self.assertEqual(resolver.call_count, 1)
			finally:
				release.set()
				if terminator.ident is not None:
					terminator.join(2)
				for worker in dnsThreads:
					worker.join(2)
			socket.assert_not_called()
			self.assertEqual(
				len(transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 1)),
				1,
			)

	def testDnsErrorsAndDeadlineReleaseTheirSlot(self):
		with patch.object(transport.socket, "getaddrinfo", side_effect=OSError("DNS failed")):
			with self.assertRaisesRegex(OSError, "DNS failed"):
				transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 1)
		with patch.object(transport, "Thread") as thread:
			thread.return_value.start.side_effect = RuntimeError("thread failed")
			with self.assertRaisesRegex(RuntimeError, "thread failed"):
				transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 1)
		with patch.object(transport.socket, "getaddrinfo", return_value=[]) as resolver:
			with self.assertRaises(TimeoutError):
				transport.resolve("test.example", 6838, threading.Event(), time.monotonic() - 1)
			resolver.assert_not_called()
			self.assertEqual(
				transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 1),
				[],
			)

	def testDnsLookupDeadlineDiscardsLateResult(self):
		entered, release = threading.Event(), threading.Event()
		workers = []

		def lookup(*args, **kwargs):
			entered.set()
			release.wait(2)
			return []

		def thread(**kwargs):
			worker = threading.Thread(**kwargs)
			workers.append(worker)
			return worker

		with (
			patch.object(transport.socket, "getaddrinfo", side_effect=lookup),
			patch.object(transport, "Thread", side_effect=thread),
		):
			try:
				with self.assertRaisesRegex(TimeoutError, "DNS lookup timed out"):
					transport.resolve("test.example", 6838, threading.Event(), time.monotonic() + 0.1)
				self.assertTrue(entered.is_set())
				self.assertFalse(release.is_set())
			finally:
				release.set()
				for worker in workers:
					worker.join(2)
			self.assertFalse(any(worker.is_alive() for worker in workers))

	def testPlaybackAlwaysUninitializesCom(self):
		for failure in (None, "create", "body", "close"):
			with self.subTest(failure=failure):
				calls = []

				def close():
					calls.append("close")
					if failure == "close":
						raise OSError("close")

				co = SimpleNamespace(
					COINIT_MULTITHREADED=0,
					CoInitializeEx=lambda flag: calls.append("initialize"),
					CoUninitialize=lambda: calls.append("uninitialize"),
				)
				wave = SimpleNamespace(
					WavePlayer=Mock(
						return_value=SimpleNamespace(
							close=close,
							enableTrimmingLeadingSilence=lambda enable: None,
						),
						side_effect=OSError("create") if failure == "create" else None,
					),
					AudioPurpose=SimpleNamespace(SPEECH=1),
				)
				with patch.dict(sys.modules, comtypes=co, nvwave=wave):
					try:
						with runtimeModule.playback(48000, 2):
							if failure == "body":
								raise OSError("body")
					except OSError as error:
						self.assertEqual(str(error), failure)
					else:
						self.assertIsNone(failure)
				self.assertEqual(
					calls,
					["initialize", *([] if failure == "create" else ["close"]), "uninitialize"],
				)

	def testRemoteTerminateWaitsForCurrentAndRetiredDeviceWorkers(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		with patch.object(serviceModule.globalVars.appArgs, "configPath", directory.name):
			owner = serviceModule.RemoteService()
		self.addCleanup(owner.terminate)
		service = owner.audio
		release = threading.Event()
		closing = [threading.Event(), threading.Event()]
		closed = [threading.Event(), threading.Event()]
		runtimes = []

		class Relay:
			identity = bytes(16)

			def __init__(self, stop):
				self.stop = stop

			def open(self, *args):
				pass

			def close(self):
				pass

			def poll(self, timeout):
				self.stop.wait(timeout)

		@contextmanager
		def playback(*args):
			index = len(runtimes) - 1
			try:
				yield SimpleNamespace(stop=lambda: None)
			finally:
				closing[index].set()
				release.wait(5)
				closed[index].set()

		def create(*args):
			runtime = runtimeModule.AudioRuntime(*args)
			runtimes.append(runtime)
			return runtime

		terminated = threading.Event()

		def terminate():
			owner.terminate()
			terminated.set()

		terminator = threading.Thread(target=terminate, daemon=True)
		with (
			patch.object(runtimeModule, "Session", Relay),
			patch.object(runtimeModule, "playback", playback),
			patch.object(audioModule, "createAudioRuntime", side_effect=create),
		):
			try:
				self.assertTrue(service.start("localhost", "master", "test"))
				self.assertTrue(service.wait_until_ready(1))
				service.stop()
				self.assertTrue(closing[0].wait(1))
				self.assertFalse(closed[0].is_set())
				self.assertTrue(service.start("localhost", "master", "test"))
				self.assertTrue(service.wait_until_ready(1))
				joining = threading.Event()
				join = service._threads[0].join

				def waitForWorker():
					joining.set()
					join()

				with patch.object(service._threads[0], "join", side_effect=waitForWorker):
					terminator.start()
					self.assertTrue(joining.wait(1))
					self.assertTrue(closing[1].wait(1))
					self.assertFalse(terminated.is_set())
					self.assertFalse(service.start("localhost", "master", "test"))
			finally:
				release.set()
				service.stop()
				if terminator.ident is not None:
					terminator.join(2)
				for runtime in runtimes:
					for worker in runtime.workers:
						worker.join(2)
		self.assertTrue(terminated.is_set())
		self.assertTrue(all(event.is_set() for event in closed))
		self.assertFalse(any(worker.is_alive() for runtime in runtimes for worker in runtime.workers))
		self.assertTrue(all(runtime.codec is None for runtime in runtimes))
		service.terminate()

	def testTerminateDuringStartingCallbackCannotLaunchWorker(self):
		service = AudioService()
		runtime = SimpleNamespace(stop=Mock())
		service.set_state_callback(lambda event: service.terminate() if event.state == "starting" else None)
		with (
			patch.object(audioModule, "createAudioRuntime", return_value=runtime),
			patch.object(audioModule, "Thread") as thread,
		):
			self.assertFalse(service.start("localhost", "master", "test"))
		thread.assert_not_called()
		runtime.stop.assert_called_once()
		self.assertEqual(service.state, "off")

	def testFailedThreadStartCanStillTerminate(self):
		service = AudioService()
		with (
			patch.object(audioModule, "createAudioRuntime", return_value=SimpleNamespace(stop=Mock())),
			patch.object(audioModule, "Thread") as thread,
		):
			thread.return_value.start.side_effect = RuntimeError("cannot start thread")
			self.assertFalse(service.start("localhost", "master", "test"))
			self.assertEqual(service.state, "error")
			service.terminate()
		thread.return_value.join.assert_not_called()


class DeviceIdTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		try:
			cls.capture = importlib.import_module("audio_runtime_test.audioCapture")
		except ImportError as error:
			raise unittest.SkipTest("NVDA comtypes/pycaw environment required") from error

	def testDeviceIdAllocationFreedOnSuccessAndFailure(self):
		ole32 = ctypes.WinDLL("ole32")
		ole32.CoTaskMemAlloc.argtypes = (ctypes.c_size_t,)
		ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
		for fails in (False, True):
			with self.subTest(fails=fails):
				allocated = []

				def getId(device, output):
					value = ctypes.create_unicode_buffer("endpoint-id")
					address = ole32.CoTaskMemAlloc(ctypes.sizeof(value))
					self.assertTrue(address)
					allocated.append(address)
					ctypes.memmove(address, value, ctypes.sizeof(value))
					ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = address
					if fails:
						raise OSError("GetId failed")

				with (
					patch.object(self.capture, "_getDeviceIdRaw", side_effect=getId),
					patch.object(self.capture, "_coTaskMemFree", wraps=self.capture._coTaskMemFree) as free,
				):
					if fails:
						with self.assertRaisesRegex(OSError, "GetId failed"):
							self.capture.getDeviceId(object())
					else:
						self.assertEqual(self.capture.getDeviceId(object()), "endpoint-id")
					free.assert_called_once()
					self.assertEqual(ctypes.cast(free.call_args.args[0], ctypes.c_void_p).value, allocated[0])

	def testSelectedDeviceFailureFallsBackAndClearsBufferedSamples(self):
		failures = (
			self.capture._SelectedEndpointUnavailable("selected device disconnected"),
			self.capture.comtypes.COMError(ctypes.c_int32(0x88890004).value, "device invalidated", None),
		)
		for failure in failures:
			with self.subTest(error=type(failure).__name__):
				attempts = []
				received = Mock()
				opened = Mock()

				def run(stop, loopback, rate, channels, ready, receive, deviceId, onOpened):
					attempts.append(deviceId)
					if deviceId is not None:
						raise failure
					onOpened("default-device")

				with (
					patch.object(self.capture, "comApartment", return_value=nullcontext()),
					patch.object(self.capture, "_capture", side_effect=run),
				):
					self.capture.capture(
						threading.Event(), True, 48000, 2, Mock(), received, "selected", opened
					)
				self.assertEqual(attempts, ["selected", None])
				received.assert_called_once_with(b"", True)
				self.assertEqual([call.args[0] for call in opened.call_args_list], ["", "default-device"])

	def testUnrelatedCaptureFailureDoesNotSwitchDevices(self):
		failures = (
			OSError("invalid capture buffer"),
			self.capture.comtypes.COMError(-1, "capture failed", None),
		)
		for failure in failures:
			with self.subTest(error=type(failure).__name__):
				received = Mock()
				opened = Mock()
				with (
					patch.object(self.capture, "comApartment", return_value=nullcontext()),
					patch.object(self.capture, "_capture", side_effect=failure) as attempt,
					self.assertRaises(type(failure)) as raised,
				):
					self.capture.capture(
						threading.Event(), True, 48000, 2, Mock(), received, "selected", opened
					)
				self.assertIs(raised.exception, failure)
				attempt.assert_called_once()
				received.assert_not_called()
				opened.assert_not_called()

	def testMissingOrInactiveSelectedEndpointAllowsFallback(self):
		for missing in (False, True):
			with self.subTest(missing=missing):
				device = Mock()
				device.GetState.return_value = 2
				enumerator = Mock()
				enumerator.GetDevice.return_value = device
				if missing:
					enumerator.GetDevice.side_effect = self.capture.comtypes.COMError(-1, "not found", None)
				with (
					patch.object(self.capture.comtypes, "CoCreateInstance", return_value=enumerator),
					self.assertRaises(self.capture._SelectedEndpointUnavailable),
				):
					self.capture._capture(
						threading.Event(), True, 48000, 2, Mock(), Mock(), "selected", Mock()
					)
				device.Activate.assert_not_called()

	def testSelectedEndpointUsesSavedId(self):
		device = Mock()
		device.GetState.return_value = 1
		client = device.Activate.return_value.QueryInterface.return_value
		client.GetBufferSize.return_value = 1024
		enumerator = Mock()
		enumerator.GetDevice.return_value = device
		stop = threading.Event()
		stop.set()
		opened = Mock()
		with (
			patch.object(self.capture.comtypes, "CoCreateInstance", return_value=enumerator),
			patch.object(self.capture, "getDeviceId", return_value="selected"),
			patch.object(self.capture.kernel32, "CreateEventW", return_value=1),
			patch.object(self.capture.kernel32, "CloseHandle"),
		):
			self.capture._capture(stop, True, 48000, 2, Mock(), Mock(), "selected", opened)
		enumerator.GetDevice.assert_called_once_with("selected")
		enumerator.GetDefaultAudioEndpoint.assert_not_called()
		opened.assert_called_once_with("selected")


if __name__ == "__main__":
	unittest.main()
