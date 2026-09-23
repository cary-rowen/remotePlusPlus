"""One libopus state per audio worker; no NVDA or device dependencies."""

import ctypes
from functools import lru_cache
from pathlib import Path

from .audioTransport import AudioError

RATE = 48000


@lru_cache(maxsize=1)
def loadLibrary() -> ctypes.CDLL:
	library = ctypes.CDLL(str(Path(__file__).resolve().parent / "lib" / "opus.dll"))
	library.opus_encoder_create.argtypes = [
		ctypes.c_int,
		ctypes.c_int,
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_int),
	]
	library.opus_encoder_create.restype = ctypes.c_void_p
	library.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
	library.opus_decoder_create.restype = ctypes.c_void_p
	for name in ("opus_encoder_destroy", "opus_decoder_destroy"):
		function = getattr(library, name)
		function.argtypes = [ctypes.c_void_p]
		function.restype = None
	# Only the fixed arguments of these cdecl variadic functions are declared.
	for name in ("opus_encoder_ctl", "opus_decoder_ctl"):
		function = getattr(library, name)
		function.argtypes = [ctypes.c_void_p, ctypes.c_int]
		function.restype = ctypes.c_int
	library.opus_encode.argtypes = [
		ctypes.c_void_p,
		ctypes.POINTER(ctypes.c_int16),
		ctypes.c_int,
		ctypes.c_void_p,
		ctypes.c_int,
	]
	library.opus_encode.restype = ctypes.c_int
	library.opus_decode.argtypes = [
		ctypes.c_void_p,
		ctypes.c_char_p,
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_int16),
		ctypes.c_int,
		ctypes.c_int,
	]
	library.opus_decode.restype = ctypes.c_int
	library.opus_packet_get_nb_samples.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
	library.opus_packet_get_nb_samples.restype = ctypes.c_int
	return library


class OpusCodec:
	def __init__(self, channels: int, bitrateKbps: int, frameMs: int, *, encoder: bool) -> None:
		super().__init__()
		if (
			type(channels) is not int
			or channels not in (1, 2)
			or type(bitrateKbps) is not int
			or bitrateKbps not in (64, 96, 192)
			or type(frameMs) is not int
			or frameMs not in (10, 20)
		):
			raise ValueError("Invalid Opus format")
		self.encoder = encoder
		self.samples = RATE * frameMs // 1000
		self.pcmBytes = self.samples * channels * 2
		self.payloadBytes = bitrateKbps * frameMs // 8
		self.lookaheadSamples = 0
		self.state = None
		self.pcm = (ctypes.c_int16 * (self.samples * channels))()
		self.packet = ctypes.create_string_buffer(self.payloadBytes)
		try:
			self.library = loadLibrary()
			error = ctypes.c_int()
			if encoder:
				# OPUS_APPLICATION_RESTRICTED_LOWDELAY
				self.state = self.library.opus_encoder_create(RATE, channels, 2051, ctypes.byref(error))
			else:
				self.state = self.library.opus_decoder_create(RATE, channels, ctypes.byref(error))
			_ = self._check(error.value)
			if not self.state:
				raise AudioError("audio_codec_failed", "Opus returned a null state")
			if encoder:
				for request, value in (
					(4002, bitrateKbps * 1000),  # OPUS_SET_BITRATE
					(4006, 0),  # OPUS_SET_VBR
					(4012, 0),  # OPUS_SET_INBAND_FEC
					(4016, 0),  # OPUS_SET_DTX
				):
					_ = self._check(self.library.opus_encoder_ctl(self.state, request, ctypes.c_int(value)))
				lookahead = ctypes.c_int()
				# OPUS_GET_LOOKAHEAD
				_ = self._check(self.library.opus_encoder_ctl(self.state, 4027, ctypes.byref(lookahead)))
				self.lookaheadSamples = lookahead.value
		except (OSError, AttributeError, AudioError) as error:
			self.close()
			raise AudioError("audio_codec_failed", str(error)) from error

	@staticmethod
	def _check(result: int) -> int:
		if result < 0:
			raise AudioError("audio_codec_failed", f"Opus error {result}")
		return result

	def encode(self, pcm: bytes) -> bytes:
		if not self.state or not self.encoder or len(pcm) != self.pcmBytes:
			raise ValueError("Invalid Opus encoder input or state")
		_ = ctypes.memmove(self.pcm, pcm, len(pcm))
		count = self._check(
			self.library.opus_encode(self.state, self.pcm, self.samples, self.packet, self.payloadBytes),
		)
		return self.packet.raw[:count]

	def decode(self, payload: bytes | None) -> bytes:
		if not self.state or self.encoder:
			raise ValueError("Invalid Opus decoder state")
		if payload is not None and (
			len(payload) != self.payloadBytes
			or self.library.opus_packet_get_nb_samples(payload, len(payload), RATE) != self.samples
		):
			raise ValueError("Invalid Opus packet size or duration")
		count = self.library.opus_decode(
			self.state,
			payload,
			len(payload) if payload else 0,
			self.pcm,
			self.samples,
			0,
		)
		if count != self.samples:
			raise ValueError(f"Invalid Opus packet: {count}")
		return ctypes.string_at(self.pcm, self.pcmBytes)

	def reset(self) -> None:
		if not self.state:
			raise ValueError("Opus codec is closed")
		control = self.library.opus_encoder_ctl if self.encoder else self.library.opus_decoder_ctl
		_ = self._check(control(self.state, 4028))  # OPUS_RESET_STATE

	def close(self) -> None:
		if self.state:
			destroy = self.library.opus_encoder_destroy if self.encoder else self.library.opus_decoder_destroy
			destroy(self.state)
			self.state = None
