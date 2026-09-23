"""Protocol, queue bounds, media fallback, mixing and playback cancellation."""

from array import array
from contextlib import contextmanager
import importlib
import itertools
import json
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


def newRuntime(bitrate=96, channels=2, frameMs=10, buffer=0, role="subscriber", sources=3):
	return runtimeModule.AudioRuntime(
		"localhost",
		6388,
		"test",
		role,
		sources,
		SimpleNamespace(bitrateKbps=bitrate, channels=channels, frameMs=frameMs, bufferMs=buffer),
		False,
	)


class RuntimeTests(unittest.TestCase):
	def testTransportHandshakeCarriesAndChecksStream(self):
		identity = bytes(range(16))
		response = (
			json.dumps(
				{
					"status": "ok",
					"role": "publisher",
					"key": "test",
					"stream": "voice_controller_to_controlled",
					"session_id": identity.hex(),
					"udp_port": 6388,
					"tcp_heartbeat_interval_ms": 5000,
					"udp_session_timeout_ms": 15000,
					"udp_audio_payload_max_bytes": 1200,
				},
			).encode()
			+ b"\n"
		)

		class FakeSocket:
			family = 2

			def __init__(self, datagram=False):
				self.datagram = datagram
				self.sent = []
				self.read = response if not datagram else b"RAS1\x01\x02" + identity

			def settimeout(self, value):
				pass

			def connect(self, address):
				self.address = address

			def sendall(self, payload):
				self.sent.append(payload)

			def recv(self, size):
				value, self.read = self.read[:size], self.read[size:]
				return value

			def send(self, payload):
				self.sent.append(payload)
				return len(payload)

			def getpeername(self):
				return ("127.0.0.1", 6838)

			def close(self):
				pass

		stop = threading.Event()
		tcp = FakeSocket()
		udp = FakeSocket(datagram=True)
		with (
			patch.object(transport, "resolve", return_value=[(2, 1, 6, "", ("127.0.0.1", 6838))]),
			patch.object(transport.socket, "socket", side_effect=[tcp, udp]),
		):
			transport.Session(stop).open(
				"localhost",
				6838,
				"test",
				"publisher",
				120,
				"voice_controller_to_controlled",
			)
		self.assertEqual(
			json.loads(tcp.sent[0]),
			{"role": "publisher", "key": "test", "stream": "voice_controller_to_controlled"},
		)

	def receiver(self, **settings):
		runtime = newRuntime(**settings)
		runtime.codec = runtimeModule.OpusCodec(
			runtime.channels,
			runtime.bitrateKbps,
			runtime.frameMs,
			encoder=False,
		)
		encoder = runtimeModule.OpusCodec(
			runtime.channels,
			runtime.bitrateKbps,
			runtime.frameMs,
			encoder=True,
		)
		self.addCleanup(runtime.codec.close)
		self.addCleanup(encoder.close)
		return runtime, encoder.encode(bytes(runtime.frameBytes))

	def testWireFormatAndMalformedPackets(self):
		identity = bytes(range(16))
		for size in (80, 120, 160, 240, 480):
			packet = transport.audioPacket(identity, 7, bytes(size))
			self.assertEqual(packet[:22], b"RAS1\1\4" + identity)
			self.assertEqual(transport.parseAudio(packet, identity, size), (7, bytes(size)))
			for malformed in (
				packet[: transport.HEADER.size],
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
		self.assertEqual(len(runtime.captureBuffers[1]), runtime.playBytes * 8)
		runtime._captured(1, b"\1\0" * (runtime.frameBytes // 2), True)
		self.assertEqual(runtime._mix(), b"\1\0" * (runtime.frameBytes // 2))
		self.assertIsNone(runtime._mix())

	def testCaptureTailWaitsForMoreAudioThenFlushesOnceDuringIdle(self):
		for frameMs, channels, sources in itertools.product((10, 20), (1, 2), (1, 2, 3)):
			with self.subTest(frameMs=frameMs, channels=channels, sources=sources):
				runtime = newRuntime(frameMs=frameMs, channels=channels, role="publisher", sources=sources)
				chunks = [bytes([i, 0]) * (runtime.frameBytes // 4) for i in range(1, 6)]
				frames = []
				with patch.object(runtimeModule.time, "monotonic", return_value=0.0) as clock:
					for i, chunk in enumerate(chunks):
						clock.return_value = (i + 1) * frameMs / 2000
						for source in runtime.captureBuffers:
							runtime._captured(source, chunk, False)
						frame = runtime._mix()
						if i % 2:
							self.assertEqual(frame, chunks[i - 1] + chunk)
							frames.append(frame)
						else:
							self.assertIsNone(frame)
					clock.return_value += frameMs / 1000 - 0.001
					self.assertIsNone(runtime._mix())
					clock.return_value += 0.002
					tail = runtime._mix()
					self.assertEqual(tail, chunks[-1] + bytes(len(chunks[-1])))
					frames.append(tail)
					self.assertEqual(b"".join(frames), b"".join(chunks) + bytes(len(chunks[-1])))
					self.assertIsNone(runtime._mix())
					clock.return_value += 1
					self.assertIsNone(runtime._mix())
					for source in runtime.captureBuffers:
						runtime._captured(source, chunks[0], True)
					self.assertIsNone(runtime._mix())
					clock.return_value += frameMs / 1000 + 0.001
					self.assertEqual(runtime._mix(), chunks[0] + bytes(len(chunks[0])))
					self.assertIsNone(runtime._mix())

	def testNaturalIdleDrainsEncoderTailOnceIncludingPartialPadding(self):
		for bitrate, channels, frameMs, paddingMs in itertools.product(
			(64, 96, 192),
			(1, 2),
			(10, 20),
			(0, 1, 5),
		):
			with self.subTest(bitrate=bitrate, channels=channels, frameMs=frameMs, paddingMs=paddingMs):
				runtime = newRuntime(
					bitrate=bitrate,
					channels=channels,
					frameMs=frameMs,
					role="publisher",
					sources=1,
				)
				encoder = runtime.codec = runtimeModule.OpusCodec(channels, bitrate, frameMs, encoder=True)
				decoder = runtimeModule.OpusCodec(channels, bitrate, frameMs, encoder=False)
				try:
					count = 48 * (frameMs - paddingMs)
					pcm = array("h", [0] * ((count - 48) * channels) + [10000] * (48 * channels)).tobytes()
					decoded = array("h")
					packets = 0
					with patch.object(runtimeModule.time, "monotonic", return_value=1.0) as clock:
						self.assertIsNone(runtime._mix())
						runtime._captured(1, pcm, False)
						clock.return_value += frameMs / 1000 + 0.001
						for _ in range(3):
							frame = runtime._mix()
							if frame is None:
								break
							decoded.extend(array("h", decoder.decode(encoder.encode(frame))))
							packets += 1
						self.assertGreater(
							max(decoded, default=0),
							3000,
							"Last millisecond of audio was lost",
						)
						self.assertEqual(packets, 1 if paddingMs == 5 else 2)
						clock.return_value += 1
						self.assertIsNone(runtime._mix(), "Idle encoder kept producing silence")
						runtime._captured(1, bytes(runtime.frameBytes), True)
						self.assertIsNotNone(runtime._mix())
						runtime.stop()
						clock.return_value += 1
						self.assertIsNone(runtime._mix(), "Cancellation flushed the encoder")
				finally:
					encoder.close()
					decoder.close()

	def testPacketLossDoesNotLeaveExtraPlaybackDelay(self):
		for bufferMs, (frameMs, lostPackets) in itertools.product(
			(0, 80),
			((10, 1), (10, 3), (20, 1), (20, 2)),
		):
			with self.subTest(bufferMs=bufferMs, frameMs=frameMs, lostPackets=lostPackets):
				runtime, encoded = self.receiver(buffer=bufferMs, frameMs=frameMs)
				baseline, _ = self.receiver(buffer=bufferMs, frameMs=frameMs)
				depths, baselineDepths = [], []
				with (
					patch.object(runtimeModule.time, "monotonic", return_value=0.0) as clock,
					patch.object(runtime.codec, "decode", wraps=runtime.codec.decode) as decode,
				):
					for tick in range(100):
						clock.return_value = tick / 200
						if tick % (frameMs // 5) == 0:
							sequence = tick // (frameMs // 5)
							packet = transport.audioPacket(bytes(16), sequence, encoded)
							baseline._receive(packet, bytes(16), lambda event: None)
							if sequence not in range(10, 10 + lostPackets):
								runtime._receive(packet, bytes(16), lambda event: None)
						baseline.buffer.pop()
						runtime.buffer.pop()
						if tick >= 70:
							depths.append(len(runtime.buffer.frames))
							baselineDepths.append(len(baseline.buffer.frames))
					self.assertEqual(sum(call.args == (None,) for call in decode.call_args_list), lostPackets)
					self.assertEqual(depths, baselineDepths, "Loss permanently increased playback latency")

	def testDeviceAudioStillPendingKeepsLossConcealment(self):
		runtime, encoded = self.receiver()
		with patch.object(runtimeModule.time, "monotonic", return_value=0.0) as clock:
			runtime._receive(transport.audioPacket(bytes(16), 0, encoded), bytes(16), lambda event: None)
			self.assertIsNotNone(runtime.buffer.pop())
			self.assertIsNotNone(runtime.buffer.pop())
			clock.return_value = 0.015
			self.assertIsNone(runtime.buffer.pop(underrun=False))
			clock.return_value = 0.02
			runtime._receive(transport.audioPacket(bytes(16), 2, encoded), bytes(16), lambda event: None)
			self.assertEqual(len(runtime.buffer.frames), 4)

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
		runtime, encoded = self.receiver()
		identity = bytes(16)
		events = []

		def receive(sequence, now):
			with patch.object(runtimeModule.time, "monotonic", return_value=now):
				runtime._receive(
					transport.audioPacket(identity, sequence, encoded),
					identity,
					events.append,
				)

		receive(transport.UINT64_MASK, 1)
		receive(0, 1.1)
		receive(0, 1.2)
		self.assertEqual(len(runtime.buffer.frames), 4)
		runtime.setMuted(True)
		receive(1, 1.3)
		self.assertTrue(runtime.receiving)
		self.assertFalse(runtime.buffer.frames)
		with patch.object(runtimeModule.time, "monotonic", return_value=2):
			runtime._receive(b"invalid", identity, events.append)
		self.assertFalse(runtime.receiving)
		runtime.setMuted(False)
		receive(0, 2.1)
		self.assertEqual(len(runtime.buffer.frames), 2)
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
		runtime, encoded = self.receiver()
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
					transport.audioPacket(bytes(16), 0, encoded),
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

	def testLossConcealmentIsBoundedAndInvalidAudioCannotRefreshMedia(self):
		for frameMs in (10, 20):
			runtime, encoded = self.receiver(frameMs=frameMs, buffer=80)
			identity = bytes(16)
			events = []

			def receive(sequence, payload=encoded):
				runtime._receive(transport.audioPacket(identity, sequence, payload), identity, events.append)

			receive(0)
			runtime.buffer.clear()
			receive(1 + 40 // frameMs)
			self.assertEqual(len(runtime.buffer.frames), (40 + frameMs) // 5)
			receive(1000000)
			self.assertEqual(len(runtime.buffer.frames), frameMs // 5)
			last = runtime.lastReceived
			for sequence, payload in ((1000000, encoded), (999999, encoded), (1000001, b"\xff")):
				receive(sequence, payload)
				self.assertEqual(runtime.lastReceived, last)
			with patch.object(runtimeModule.time, "monotonic", return_value=last + 0.6):
				receive(1000002, b"\xff")
			self.assertFalse(runtime.receiving)
			self.assertFalse(runtime.buffer.frames)
			self.assertEqual([e["receiving"] for e in events], [True, False])

	def testCaptureAndPlaybackBoundsDoNotGrowWithPacketDuration(self):
		for frameMs in (10, 20):
			runtime = newRuntime(frameMs=frameMs, role="publisher", sources=1)
			runtime._captured(1, bytes(runtime.frameBytes * 100), False)
			self.assertEqual(len(runtime.captureBuffers[1]), 48000 * 4 * 40 // 1000)
			self.assertEqual(runtime.frameBytes, 48000 * 4 * frameMs // 1000)
			self.assertEqual(runtime.buffer.frames.maxlen * 5, 40)

	def testMuteDuringDecodeDiscardsOldAudioEvenAfterLargeGap(self):
		for gap in (0, 100):
			runtime, encoded = self.receiver()
			runtime.nextSequence = 0
			decode = runtime.codec.decode

			def muteWhileDecoding(payload):
				pcm = decode(payload)
				runtime.setMuted(True)
				runtime.setMuted(False)
				return pcm

			with patch.object(runtime.codec, "decode", side_effect=muteWhileDecoding):
				runtime._receive(
					transport.audioPacket(bytes(16), gap, encoded),
					bytes(16),
					lambda event: None,
				)
			self.assertFalse(runtime.buffer.frames)

	def testMuteAndStopCompleteWhileLargeGapDecodeIsBlocked(self):
		for action in ("mute", "stop"):
			with self.subTest(action=action):
				runtime, encoded = self.receiver()
				runtime.nextSequence = 0
				decoding, release = threading.Event(), threading.Event()
				completed = threading.Event()
				errors = []
				decode = runtime.codec.decode

				def blockedDecode(payload):
					decoding.set()
					if not release.wait(2):
						raise TimeoutError("Decode was not released")
					return decode(payload)

				def receive():
					try:
						runtime._receive(
							transport.audioPacket(bytes(16), 100, encoded),
							bytes(16),
							lambda event: None,
						)
					except Exception as error:
						errors.append(error)

				def cancel():
					if action == "mute":
						runtime.setMuted(True)
						runtime.setMuted(False)
					else:
						runtime.stop()
					completed.set()

				with patch.object(runtime.codec, "decode", side_effect=blockedDecode):
					receiver = threading.Thread(target=receive, daemon=True)
					control = threading.Thread(target=cancel, daemon=True)
					receiver.start()
					try:
						self.assertTrue(decoding.wait(1))
						control.start()
						self.assertTrue(completed.wait(1), "Audio control waited for decoding")
					finally:
						release.set()
						receiver.join(2)
						if control.ident is not None:
							control.join(2)
				self.assertFalse(receiver.is_alive())
				self.assertFalse(control.is_alive())
				self.assertFalse(errors)
				self.assertFalse(runtime.buffer.frames, "Cancelled audio was queued after decode")

	def testCodecIsReleasedWhenConnectionFailsBeforeCaptureStarts(self):
		runtime = newRuntime(role="publisher")
		runtime.stream = "voice_controller_to_controlled"
		codec = runtimeModule.OpusCodec(2, 96, 10, encoder=True)
		self.addCleanup(codec.close)
		events = []
		with (
			patch.object(runtimeModule, "OpusCodec", return_value=codec),
			patch.object(
				transport.Session,
				"open",
				side_effect=transport.AudioError("control_connection_failed", "test"),
			) as connect,
		):
			runtime._run(events.append)
		self.assertIsNone(codec.state)
		self.assertIsNone(runtime.codec)
		self.assertFalse(runtime.workers)
		self.assertEqual(events[-1]["code"], "control_connection_failed")
		connect.assert_called_once_with(
			runtime.host,
			runtime.port,
			runtime.key,
			runtime.role,
			runtime.payloadBytes,
			runtime.stream,
		)

	def testCancelledRuntimeNeverConnects(self):
		runtime = newRuntime()
		runtime.stop()
		with patch.object(transport.Session, "open") as connect:
			runtime.run(lambda event: self.fail(str(event)))
		connect.assert_not_called()


if __name__ == "__main__":
	unittest.main()
