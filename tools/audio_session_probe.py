"""Exercise Remote negotiation and Opus streaming through a live server.

Run with NVDA's development Python. Uses the real NVDA transport, extension
points and add-on services in a random room. Capture supplies a synthetic tone;
playback counts decoded PCM without opening devices or recording audio.
"""

import argparse
import importlib
import json
import logging
import math
import queue
import sys
import tempfile
import threading
import time
import uuid
from array import array
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--host", required=True)
parser.add_argument("--nvda-source", type=Path, default=Path("D:/git/nvda/source"))
parser.add_argument(
	"--insecure",
	action="store_true",
	help="Accept a test control server certificate without verification",
)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
NVDA = args.nvda_source
sys.path.insert(0, str(ROOT / "tests"))
negotiation = importlib.import_module("test_audio_negotiation")
serviceModule, audioModule = negotiation.serviceModule, negotiation.audioModule
runtimeModule = importlib.import_module("test_audio_runtime").runtimeModule

log = logging.getLogger("probe")
logging.basicConfig(level=logging.ERROR)
log.debugWarning = log.debug
sys.modules["logHandler"] = SimpleNamespace(log=log)
sys.path.append(str(NVDA))
extensionPoints = importlib.import_module("extensionPoints")

pending = queue.Queue()


def post(fn, *args, **kwargs):
	pending.put((fn, args, kwargs))


sys.modules["wx"] = SimpleNamespace(CallAfter=post)
speech = ModuleType("speech")
speech.commands = SimpleNamespace(
	SynthCommand=type("SynthCommand", (), {}),
	EndUtteranceCommand=type("EndUtteranceCommand", (), {}),
)
sys.modules.update({"speech": speech, "speech.commands": speech.commands})
pkg = ModuleType("probe_remote")
pkg.__path__ = [str(NVDA / "_remoteClient")]
sys.modules[pkg.__name__] = pkg
sys.modules["probe_remote.configuration"] = SimpleNamespace(_isDebugForRemoteClient=lambda: False)
sys.modules["probe_remote.connectionInfo"] = SimpleNamespace(ConnectionInfo=object)
transportModule = importlib.import_module("probe_remote.transport")
serializerModule = importlib.import_module("probe_remote.serializer")
serviceModule.callWithSupportedKwargs = extensionPoints.callWithSupportedKwargs
serviceModule.queueHandler.queueFunction = lambda _, fn, *args, **kwargs: post(fn, *args, **kwargs)
host = args.host
room = uuid.uuid4().hex
peers = []
threads = []
stats = {"bytes": 0, "nonzero": 0, "packets": 0, "discarded": 0}
drop = threading.Event()
silence = threading.Event()


def pumpUntil(predicate, timeout=10):
	end = time.monotonic() + timeout
	while not predicate():
		if time.monotonic() >= end:
			raise TimeoutError(
				[
					(p.audio.state, p.audio.error, p._audioRequestPending, len(p._audioFollowers))
					for p in peers
				],
			)
		try:
			fn, args, kwargs = pending.get(timeout=0.01)
		except queue.Empty:
			continue
		fn(*args, **kwargs)


def pump(seconds):
	end = time.monotonic() + seconds
	pumpUntil(lambda: time.monotonic() >= end, seconds + 1)


def capture(stop, loopback, rate, channels, ready, receive):
	pcm = array(
		"h",
		(
			int(2000 * math.sin(2 * math.pi * 1000 * i / rate))
			for i in range(rate // 100)
			for _ in range(channels)
		),
	).tobytes()
	ready()
	while not stop.wait(0.01):
		if not silence.is_set():
			receive(pcm, False)


class Player:
	def feed(self, pcm, onDone=None):
		stats["bytes"] += len(pcm)
		if any(pcm):
			stats["nonzero"] += 1
		if onDone:
			onDone()

	def stop(self):
		pass


@contextmanager
def playback(*args):
	yield Player()


def runtime(*args):
	r = runtimeModule.AudioRuntime(*args)
	if r.role == "subscriber":
		receive = r._receive

		def intercept(packet, *args):
			if packet and len(packet) > 38:
				stats["packets"] += 1
				if drop.is_set():
					stats["discarded"] += 1
					packet = None
			receive(packet, *args)

		r._receive = intercept
	return r


def peer(mode, directory):
	with patch.object(serviceModule.globalVars.appArgs, "configPath", directory):
		p = serviceModule.RemoteService()
	p.getCurrentConnectionInfo = lambda: SimpleNamespace(mode=mode, hostname=host, key=room)
	t = transportModule.RelayTransport(
		serializerModule.JSONSerializer(),
		(host, 6837),
		channel=room,
		connectionType=mode,
		insecure=args.insecure,
	)
	local = SimpleNamespace(isMuted=False, speak=Mock(), beep=Mock(), playWave=Mock())
	session = SimpleNamespace(transport=t, localMachine=local, followers=set(), leaders={})
	client = SimpleNamespace(
		leaderSession=session if mode == "master" else None,
		followerSession=session if mode == "slave" else None,
		localMachine=local,
		_doToggleMute=lambda: None,
	)
	p.getClient = lambda: client

	def joined(clients=None, client=None, **kwargs):
		for c in clients or ([client] if client else []):
			if c["connection_type"] == "master":
				session.leaders[c["id"]] = {}
			else:
				session.followers.add(c["id"])

	t.registerInbound("channel_joined", joined)
	t.registerInbound("client_joined", joined)
	p._probeJoined = joined
	if mode == "master":
		for kind, handler in [("speak", local.speak), ("tone", local.beep), ("wave", local.playWave)]:
			t.registerInbound(kind, handler)
	p.handleRemoteConnectionChanged(True)
	th = threading.Thread(target=t.run, daemon=True)
	threads.append(th)
	peers.append(p)
	th.start()
	return p


with (
	tempfile.TemporaryDirectory() as directory,
	patch.object(audioModule, "createAudioRuntime", side_effect=runtime),
	patch.object(runtimeModule, "playback", playback),
	patch.dict(sys.modules, {"audio_runtime_test.audioCapture": SimpleNamespace(capture=capture)}),
):
	try:
		follower = peer("slave", directory)
		leader = peer("master", directory)
		pumpUntil(
			lambda: len(leader._audioFollowers) == 1 and bool(follower.getClient().followerSession.leaders),
		)
		print("control joined, real NVDA transport and extension points", flush=True)
		for bitrate in (64, 96, 192):
			for channels in (1, 2):
				for frame in (10, 20):
					settings = audioModule.AudioSettings(0, bitrate, channels, frame)
					assert leader.connection_manager.setAudioSettings(settings)
					assert leader.requestAudioSources(1)
					pumpUntil(lambda: leader.audio.state == "on" and leader.audio.isReceiving)
					before = dict(stats)
					pump(0.35)
					assert stats["nonzero"] > before["nonzero"]
					assert follower.audio.settings == settings
					leader.audio.setMuted(True)
					beforeMute = stats["bytes"]
					pump(0.1)
					assert stats["bytes"] == beforeMute
					leader.audio.setMuted(False)
					pumpUntil(lambda: stats["bytes"] > beforeMute)
					print(
						json.dumps(
							{
								"bitrate": bitrate,
								"channels": channels,
								"frame_ms": frame,
								"decoded_bytes": stats["bytes"] - before["bytes"],
								"packets": stats["packets"] - before["packets"],
							},
						),
						flush=True,
					)
					leader.stopAudio()
					pumpUntil(lambda: leader.audio.state == "off" and follower.audio.state == "off")
		# Exercise a >40 ms packet gap while controls run.
		assert leader.requestAudioSources(3)
		pumpUntil(lambda: leader.audio.isReceiving)
		drop.set()
		pump(0.15)
		assert stats["discarded"] > 0
		drop.clear()
		before = stats["bytes"]
		pumpUntil(lambda: stats["bytes"] > before)
		# Long media gap restores speech, reception recovers.
		silence.set()
		pumpUntil(lambda: not leader.audio.isReceiving, 2)
		silence.clear()
		pumpUntil(lambda: leader.audio.isReceiving)
		leader.stopAudio()
		pumpUntil(lambda: leader.audio.state == "off" and follower.audio.state == "off")
		print("gap, mute, media fallback, resume, remote cancellation: passed", flush=True)
	finally:
		for p in peers:
			p.terminate()
		for p in peers:
			p._getAudioTransport().close()
		for t in threads:
			t.join(3)
		assert not any(t.is_alive() for t in threads)
		assert not any(t.name.startswith("remotePlusPlusAudio") for t in threading.enumerate())
		print("all control/audio workers stopped", flush=True)
