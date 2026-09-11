"""WASAPI capture; all COM objects and buffers belong to the capture thread."""

import ctypes
from ctypes import POINTER, c_ubyte, c_uint32, c_uint64
from ctypes.wintypes import DWORD, HANDLE

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX
from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
import time
from typing import Any
from threading import Event
from collections.abc import Callable

from .audioCom import comApartment


class IAudioCaptureClient(IUnknown):
	_iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
	_methods_ = [
		COMMETHOD(
			[],
			HRESULT,
			"GetBuffer",
			(["out"], POINTER(POINTER(c_ubyte)), "data"),
			(["out"], POINTER(c_uint32), "frames"),
			(["out"], POINTER(DWORD), "flags"),
			(["out"], POINTER(c_uint64), "position"),
			(["out"], POINTER(c_uint64), "timestamp"),
		),
		COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "frames")),
		COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "frames")),
	]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
kernel32.CreateEventW.restype = HANDLE
kernel32.WaitForSingleObject.argtypes = (HANDLE, DWORD)
kernel32.WaitForSingleObject.restype = DWORD
kernel32.CloseHandle.argtypes = (HANDLE,)
kernel32.CloseHandle.restype = ctypes.c_int

# IMMDevice::GetId is vtable slot 5 (including IUnknown). Bypass pycaw's
# automatic LPWSTR-to-str conversion so its CoTaskMem allocation can be freed.
_getDeviceIdRaw = ctypes.WINFUNCTYPE(HRESULT, POINTER(ctypes.c_wchar_p))(5, "GetId", None)
_coTaskMemFree = ctypes.WinDLL("ole32").CoTaskMemFree
_coTaskMemFree.argtypes = (ctypes.c_void_p,)
_coTaskMemFree.restype = None


def getDeviceId(device: Any) -> str:
	pointer = ctypes.c_wchar_p()
	try:
		_getDeviceIdRaw(device, ctypes.byref(pointer))
		value = pointer.value
		if value is None:
			raise OSError("Audio endpoint returned no device ID")
		return value
	finally:
		_coTaskMemFree(pointer)


def capture(
	stop: Event,
	loopback: bool,
	rate: int,
	channels: int,
	ready: Callable[[], None],
	received: Callable[[bytes, bool], None],
) -> None:
	with comApartment():
		_capture(stop, loopback, rate, channels, ready, received)


def _capture(
	stop: Event,
	loopback: bool,
	rate: int,
	channels: int,
	ready: Callable[[], None],
	received: Callable[[bytes, bool], None],
) -> None:
	enumerator: Any = None
	device: Any = None
	client: Any = None
	reader: Any = None
	event = None
	started = False
	try:
		enumerator = comtypes.CoCreateInstance(
			GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
			interface=IMMDeviceEnumerator,
			clsctx=comtypes.CLSCTX_INPROC_SERVER,
		)
		flow = 0 if loopback else 1
		device = enumerator.GetDefaultAudioEndpoint(flow, 0)
		deviceId = getDeviceId(device)
		client = device.Activate(IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(IAudioClient)
		format = WAVEFORMATEX()
		format.wFormatTag = 1
		format.nChannels = channels
		format.nSamplesPerSec = rate
		format.wBitsPerSample = 16
		format.nBlockAlign = channels * 2
		format.nAvgBytesPerSec = rate * format.nBlockAlign
		# Windows' shared engine performs channel/rate conversion in native code.
		# EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY, plus LOOPBACK.
		flags = 0x00040000 | 0x80000000 | 0x08000000 | (0x00020000 if loopback else 0)
		client.Initialize(0, flags, 0, 0, ctypes.byref(format), None)
		capacity = client.GetBufferSize()
		reader = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
		event = kernel32.CreateEventW(None, False, False, None)
		if not event:
			raise ctypes.WinError(ctypes.get_last_error())
		client.SetEventHandle(event)
		client.Start()
		started = True
		ready()
		nextDeviceCheck = time.monotonic() + 1
		while not stop.is_set():
			result = kernel32.WaitForSingleObject(event, 100)
			if result == 0xFFFFFFFF:
				raise ctypes.WinError(ctypes.get_last_error())
			if time.monotonic() >= nextDeviceCheck:
				if getDeviceId(enumerator.GetDefaultAudioEndpoint(flow, 0)) != deviceId:
					raise OSError("Default capture endpoint changed; restart audio to follow it")
				nextDeviceCheck = time.monotonic() + 1
			while not stop.is_set() and reader.GetNextPacketSize():
				data, frames, packetFlags, _, _ = reader.GetBuffer()
				try:
					if frames > capacity:
						raise OSError("Capture buffer exceeds negotiated device capacity")
					length = frames * channels * 2
					pcm = bytes(length) if packetFlags & 2 else ctypes.string_at(data, length)
				finally:
					reader.ReleaseBuffer(frames)
				received(pcm, bool(packetFlags & 1))
	finally:
		try:
			if started:
				client.Stop()
		finally:
			if event:
				_ = kernel32.CloseHandle(event)
			# Drop COM references before the apartment is uninitialized, even on error.
			reader = client = device = enumerator = None
