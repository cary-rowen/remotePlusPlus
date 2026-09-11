"""Protocol, queue bounds, media fallback, mixing and playback cancellation."""

from array import array
from contextlib import contextmanager
import importlib
import random
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

package = ModuleType("audio_runtime_test")
package.__path__ = [str(Path(__file__).parents[1] / "addon/globalPlugins/remotePlusPlus")]
sys.modules[package.__name__] = package
runtimeModule = importlib.import_module("audio_runtime_test.audioRuntime")
transport = importlib.import_module("audio_runtime_test.audioTransport")


def newRuntime(quality="48000_stereo", buffer=0, role="subscriber", sources=3):
	return runtimeModule.AudioRuntime(
		"localhost",
		6388,
		"test",
		role,
		sources,
		SimpleNamespace(quality=quality, bufferMs=buffer),
		False,
	)


class RuntimeTests(unittest.TestCase):
	def testWireFormatAndMalformedPackets(self):
		identity = bytes(range(16))
		for quality, size in (
			("48000_stereo", 960),
			("48000_mono", 480),
			("24000_mono", 240),
			("16000_mono", 160),
		):
			runtime = newRuntime(quality)
			self.assertEqual(runtime.frameBytes, size)
			packet = transport.audioPacket(identity, 7, bytes(size))
			self.assertEqual(packet[:22], b"RAS1\1\4" + identity)
			self.assertEqual(transport.parseAudio(packet, identity, size), (7, bytes(size)))
			for malformed in (
				packet[:-1],
				packet + b"x",
				b"bad" + packet[3:],
				packet[:4] + b"\2" + packet[5:],
			):
				self.assertIsNone(transport.parseAudio(malformed, identity, size))
			self.assertIsNone(transport.parseAudio(packet, bytes(16), size))
		with self.assertRaises(ValueError):
			transport.audioPacket(identity, 0, bytes(1201))

	def testMixFixedGainAndFastSingleSource(self):
		left = array("h", [32767, -32768, 100, -100]).tobytes()
		right = array("h", [-32767, -32768, 200, 100]).tobytes()
		self.assertEqual(array("h", runtimeModule.mixPcm(left, right)).tolist(), [0, -32768, 150, 0])
		runtime = newRuntime(role="publisher")
		frame = array("h", [2000] * (runtime.frameBytes // 2)).tobytes()
		runtime._captured(1, frame, False)
		self.assertEqual(set(array("h", runtime._mix())), {1000})
		runtime = newRuntime(role="publisher", sources=1)
		runtime._captured(1, frame, False)
		self.assertEqual(runtime._mix(), frame)

	def testPackedMixMatchesSignedArithmeticWithoutCrossLaneCarry(self):
		randomizer = random.Random(71)
		edges = [-32768, -32767, -1, 0, 1, 32766, 32767]
		pairs = [(a, b) for a in edges for b in edges]
		pairs.extend(
			(randomizer.randrange(-32768, 32768), randomizer.randrange(-32768, 32768)) for _ in range(10000)
		)
		left = array("h", (a for a, _ in pairs)).tobytes()
		right = array("h", (b for _, b in pairs)).tobytes()
		self.assertEqual(
			array("h", runtimeModule.mixPcm(left, right)).tolist(),
			[(a + b) // 2 for a, b in pairs],
		)
		self.assertEqual(runtimeModule.mixPcm(b"", b""), b"")
		with self.assertRaises(ValueError):
			runtimeModule.mixPcm(b"odd", b"odd")

	def testCaptureOverflowAndDiscontinuityDiscardOldAudio(self):
		runtime = newRuntime(role="publisher", sources=1)
		runtime._captured(1, bytes(runtime.frameBytes * 20), False)
		self.assertEqual(len(runtime.captureBuffers[1]), runtime.frameBytes * 8)
		runtime._captured(1, b"\1\0" * (runtime.frameBytes // 2), True)
		self.assertEqual(runtime._mix(), b"\1\0" * (runtime.frameBytes // 2))
		self.assertIsNone(runtime._mix())

	def testPlaybackPrebufferUnderrunAndOverflow(self):
		for bufferMs in (0, 10, 20, 40, 80):
			buffer = runtimeModule.PlaybackBuffer(bufferMs)
			self.assertIsNone(buffer.pop())
			for i in range(buffer.target - 1):
				buffer.frames.append(i)
				self.assertIsNone(buffer.pop())
			buffer.frames.append(99)
			self.assertIsNotNone(buffer.pop())
			while buffer.pop() is not None:
				pass
			self.assertFalse(buffer.started)
			buffer.frames.extend(range(100))
			self.assertEqual(len(buffer.frames), bufferMs // 5 + 8)
			self.assertEqual(buffer.frames[-1], 99)

	def testMediaExpirySequenceWrapAndMute(self):
		runtime = newRuntime()
		identity = bytes(16)
		events = []

		def receive(sequence, now):
			with patch.object(runtimeModule.time, "monotonic", return_value=now):
				runtime._receive(
					transport.audioPacket(identity, sequence, bytes(runtime.frameBytes)),
					identity,
					events.append,
				)

		receive(transport.UINT64_MASK, 1)
		receive(0, 1.1)
		receive(0, 1.2)
		self.assertEqual(len(runtime.buffer.frames), 2)
		runtime.setMuted(True)
		receive(1, 1.3)
		self.assertTrue(runtime.receiving)
		self.assertFalse(runtime.buffer.frames)
		with patch.object(runtimeModule.time, "monotonic", return_value=2):
			runtime._receive(b"invalid", identity, events.append)
		self.assertFalse(runtime.receiving)
		runtime.setMuted(False)
		receive(0, 2.1)
		self.assertEqual(len(runtime.buffer.frames), 1)
		self.assertEqual([event["receiving"] for event in events], [True, False, True])

	def testQueuedDeviceAudioDoesNotTriggerPrematureRebuffer(self):
		buffer = runtimeModule.PlaybackBuffer(10)
		buffer.frames.extend([b"a", b"b"])
		self.assertEqual(buffer.pop(), b"a")
		self.assertEqual(buffer.pop(), b"b")
		self.assertIsNone(buffer.pop(underrun=False))
		buffer.frames.append(b"c")
		self.assertEqual(buffer.pop(underrun=False), b"c")
		self.assertIsNone(buffer.pop(underrun=True))
		buffer.frames.append(b"d")
		self.assertIsNone(buffer.pop())

	def testPlaybackMuteAndStopWithRealWorker(self):
		runtime = newRuntime()
		fed = threading.Event()
		played = []

		class Player:
			def feed(self, payload, onDone=None):
				if payload:
					played.append(payload)
					fed.set()
				if onDone:
					onDone()

			def stop(self):
				pass

		@contextmanager
		def player(*args):
			yield Player()

		with patch.object(runtimeModule, "playback", player):
			thread = threading.Thread(target=runtime._play)
			thread.start()
			try:
				with runtime.condition:
					runtime.buffer.frames.append(b"first")
					runtime.condition.notify_all()
				self.assertTrue(fed.wait(1))
				runtime.setMuted(True)
				runtime._receive(
					transport.audioPacket(bytes(16), 0, bytes(runtime.frameBytes)),
					bytes(16),
					lambda event: None,
				)
				self.assertFalse(runtime.buffer.frames)
			finally:
				runtime.stop()
				thread.join(1)
		self.assertFalse(thread.is_alive())
		self.assertEqual(played, [b"first"])

	def testInvalidHandshakeFields(self):
		for value in (True, 0, -1, "6388", None, 65536):
			with self.assertRaises(ValueError):
				transport.Session._number({"port": value}, "port", 1, 65535)

	def testCancelledRuntimeNeverConnects(self):
		runtime = newRuntime()
		runtime.stop()
		with patch.object(transport.Session, "open") as connect:
			runtime.run(lambda event: self.fail(str(event)))
		connect.assert_not_called()


if __name__ == "__main__":
	unittest.main()
