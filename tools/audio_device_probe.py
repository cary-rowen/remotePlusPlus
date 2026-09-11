"""Hardware probe using real NVDA WavePlayer/WASAPI with only UI/config stubs.

Run with D:/git/nvda/.venv/Scripts/python.exe. This workstation probe uses the
NVDA source checkout and installed 2026.3beta1 helper DLL. It plays quiet test
sines, counts captured bytes, checks mute/stop, and sends nothing to a server.
It is not a substitute for listening on two different computers.
"""

import importlib.util
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from array import array


def main():
	ROOT = Path(__file__).resolve().parents[1]
	NVDA = Path("D:/git/nvda/source")
	DLL = Path("C:/Program Files/NVDA/lib/2026.3beta1/x64/nvdaHelperLocal.dll")
	dllDir = os.add_dll_directory(str(DLL.parent))

	class Action:
		def register(self, *a):
			pass

		def unregister(self, *a):
			pass

	class Conf(dict):
		def getConfigValidation(self, setting):
			return SimpleNamespace(default="default")

	log = logging.getLogger("probe")
	log.debugWarning = log.debug
	sys.modules.update(
		{
			"NVDAState": SimpleNamespace(ReadPaths=SimpleNamespace(nvdaHelperLocalDll=str(DLL))),
			"config": SimpleNamespace(
				conf=Conf(
					audio={"audioAwakeTime": 0, "soundVolume": 0, "soundVolumeFollowsVoice": False},
					speech={"trimLeadingSilence": True},
				),
			),
			"garbageHandler": SimpleNamespace(TrackedObject=object),
			"logHandler": SimpleNamespace(log=log, getOnErrorSoundRequested=Action),
			"extensionPoints": SimpleNamespace(Decider=Action),
			"core": SimpleNamespace(callLater=lambda *a: None),
			"globalVars": SimpleNamespace(),
			"speech": SimpleNamespace(SpeechSequence=list),
			"speech.commands": SimpleNamespace(BreakCommand=type("BreakCommand", (), {})),
			"synthDriverHandler": SimpleNamespace(
				pre_synthSpeak=Action(),
				getSynth=lambda: SimpleNamespace(volume=0, isSupported=lambda setting: setting == "volume"),
			),
			"utils": SimpleNamespace(
				_deprecate=SimpleNamespace(MovedSymbol=lambda *a: None, handleDeprecations=lambda *a: None),
			),
			"winBindings": ModuleType("winBindings"),
		},
	)

	def load(name, path):
		spec = importlib.util.spec_from_file_location(name, path)
		mod = importlib.util.module_from_spec(spec)
		sys.modules[name] = mod
		spec.loader.exec_module(mod)
		return mod

	load("winBindings.mmeapi", NVDA / "winBindings/mmeapi.py")
	load("wasapi", NVDA / "wasapi.py")
	nvwave = load("nvwave", NVDA / "nvwave.py")
	nvwave.initialize()
	setChannelVolume = nvwave.wasapi.wasPlay_setChannelVolume

	def checkChannelVolume(player, channel, volume):
		assert volume.value == 1.0, "Local sound settings changed remote audio volume"
		return setChannelVolume(player, channel, volume)

	nvwave.wasapi.wasPlay_setChannelVolume = checkChannelVolume
	pkg = ModuleType("probe_audio")
	pkg.__path__ = [str(ROOT / "addon/globalPlugins/remotePlusPlus")]
	sys.modules["probe_audio"] = pkg
	from probe_audio.audioRuntime import AudioRuntime
	from probe_audio.audioCapture import capture
	from probe_audio.audioTransport import audioPacket

	for quality in ("48000_stereo", "48000_mono", "24000_mono", "16000_mono"):
		runtime = AudioRuntime(
			"unused",
			6838,
			"test",
			"subscriber",
			1,
			SimpleNamespace(quality=quality, bufferMs=0),
			False,
		)
		worker = threading.Thread(target=runtime._worker, args=(runtime._play,))
		worker.start()
		with runtime.condition:
			runtime.condition.wait_for(lambda: runtime.readyCount or runtime.error, 3)
		if runtime.error:
			raise runtime.error
		assert not runtime.player._enableTrimmingLeadingSilence
		counts = {True: 0, False: 0}

		def captured(loopback, pcm, gap):
			counts[loopback] += len(pcm)

		for loopback in (True, False):
			runtime._startWorker(
				capture,
				runtime.stopping,
				loopback,
				runtime.rate,
				runtime.channels,
				runtime._ready,
				lambda pcm, gap, loopback=loopback: captured(loopback, pcm, gap),
			)
		with runtime.condition:
			runtime.condition.wait_for(lambda: runtime.readyCount == 3 or runtime.error, 3)
		sid = bytes(16)
		sequence = 0
		for muted, followsVoice in ((False, False), (True, False), (False, True)):
			nvwave.config.conf["audio"]["soundVolumeFollowsVoice"] = followsVoice
			nvwave.config.conf["audio"]["soundVolume"] = 100 if followsVoice else 0
			runtime.setMuted(muted)
			assert not runtime.player._enableTrimmingLeadingSilence
			until = time.monotonic() + 1.0
			deadline = time.monotonic()
			while time.monotonic() < until:
				pcm = array(
					"h",
					(
						int(
							500
							* math.sin(
								2 * math.pi * 997 * (sequence * runtime.rate // 200 + i) / runtime.rate,
							),
						)
						for i in range(runtime.rate // 200)
						for ch in range(runtime.channels)
					),
				).tobytes()
				runtime._receive(audioPacket(sid, sequence, pcm), sid, lambda e: None)
				sequence += 1
				deadline += 0.005
				time.sleep(max(0, deadline - time.monotonic()))
			if runtime.error:
				raise runtime.error
		start = time.monotonic()
		runtime.stop()
		worker.join(2)
		for captureWorker in runtime.workers:
			captureWorker.join(2)
			assert not captureWorker.is_alive()
		assert all(counts.values()), counts
		print("captured bytes (system, microphone)", counts, flush=True)
		assert not worker.is_alive()
		if runtime.error:
			raise runtime.error
		print(
			quality,
			"real NVDA WavePlayer feed/mute/stop passed",
			"stop_ms",
			round((time.monotonic() - start) * 1000, 2),
			flush=True,
		)
	nvwave.terminate()
	dllDir.close()


if __name__ == "__main__":
	main()
