"""Exercise the bundled native codec; a missing DLL must fail the build."""

from array import array
import ctypes
import itertools
import math
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from test_audio_runtime import runtimeModule

OpusCodec = runtimeModule.OpusCodec


class CodecTests(unittest.TestCase):
	def testAllFormatsRoundTripWithExpectedBitrateAndIndependentChannels(self):
		for bitrate, channels, duration in itertools.product((64, 96, 192), (1, 2), (10, 20)):
			with self.subTest(bitrate=bitrate, channels=channels, duration=duration):
				encoder = OpusCodec(channels, bitrate, duration, encoder=True)
				decoder = OpusCodec(channels, bitrate, duration, encoder=False)
				try:
					decoded = array("h")
					for frame in range(10):
						pcm = array(
							"h",
							(
								int(
									10000
									* math.sin(
										2
										* math.pi
										* (440 if channel == 0 else 880)
										* (frame * encoder.samples + i)
										/ 48000,
									),
								)
								for i in range(encoder.samples)
								for channel in range(channels)
							),
						)
						packet = encoder.encode(pcm.tobytes())
						self.assertEqual(len(packet), bitrate * duration // 8)
						decoded = array("h", decoder.decode(packet))
						self.assertEqual(len(decoded), encoder.samples * channels)
						self.assertGreater(max(decoded), 1000)
					if channels == 2:
						self.assertNotEqual(decoded[::2], decoded[1::2])
					self.assertEqual(len(decoder.decode(None)), encoder.pcmBytes)
					decoder.reset()
					self.assertEqual(len(decoder.decode(packet)), encoder.pcmBytes)
				finally:
					encoder.close()
					decoder.close()
				self.assertIsNone(encoder.state)
				self.assertIsNone(decoder.state)
				decoder.close()

	def testInvalidPacketsAndClosedStates(self):
		encoder = OpusCodec(2, 96, 20, encoder=True)
		decoder = OpusCodec(2, 96, 10, encoder=False)
		self.addCleanup(encoder.close)
		self.addCleanup(decoder.close)
		packet = encoder.encode(bytes(encoder.pcmBytes))
		for invalid in (b"", b"\xff" * decoder.payloadBytes, bytes(1201), packet[: decoder.payloadBytes]):
			with self.assertRaises(ValueError):
				decoder.decode(invalid)
		with self.assertRaises(ValueError):
			encoder.encode(b"odd")
		decoder.close()
		with self.assertRaises(ValueError):
			decoder.decode(packet)

	def testMissingLibraryProducesSpecificError(self):
		with patch.dict(
			OpusCodec.__init__.__globals__,
			loadLibrary=lambda: ctypes.CDLL("missing-remotePlusPlus-opus.dll"),
		):
			with self.assertRaises(runtimeModule.AudioError) as error:
				OpusCodec(2, 96, 10, encoder=True)
		self.assertEqual(error.exception.code, "audio_codec_failed")

	def testBundledLibraryIsX64AndHasLicense(self):
		folder = Path(runtimeModule.__file__).parent / "lib"
		data = (folder / "opus.dll").read_bytes()
		pe = struct.unpack_from("<I", data, 0x3C)[0]
		self.assertEqual(data[pe : pe + 4], b"PE\0\0")
		self.assertEqual(struct.unpack_from("<H", data, pe + 4)[0], 0x8664)
		self.assertIn("Redistribution", (folder / "COPYING.opus").read_text(encoding="utf-8"))
