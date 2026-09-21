"""Measure an Opus publisher against a live relay in a fresh isolated room.

Run with NVDA's development Python (comtypes/pycaw already installed).
Only aggregate counts are saved; raw captured audio and room keys are not logged.
"""

import argparse
import ctypes
from ctypes import wintypes
import importlib
import json
import os
import sysconfig
from pathlib import Path
import queue
import socket
import statistics
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType("probe_audio")
package.__path__ = [str(ROOT / "addon/globalPlugins/remotePlusPlus")]
sys.modules[package.__name__] = package
runtimeModule = importlib.import_module("probe_audio.audioRuntime")
transport = importlib.import_module("probe_audio.audioTransport")


def processCpu(process):
	getTimes = ctypes.windll.kernel32.GetProcessTimes
	getTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
	getTimes.restype = wintypes.BOOL
	times = [wintypes.FILETIME() for _ in range(4)]
	if not getTimes(int(process._handle), *(ctypes.byref(value) for value in times)):
		raise ctypes.WinError()
	return sum((value.dwHighDateTime << 32) + value.dwLowDateTime for value in times[2:]) / 10_000_000


def child(args):
	runtime = runtimeModule.AudioRuntime(
		args.host,
		args.port,
		args.key,
		"publisher",
		args.sources,
		SimpleNamespace(bitrateKbps=args.bitrate, channels=args.channels, frameMs=args.frame_ms, bufferMs=0),
		False,
	)

	def commands():
		for line in sys.stdin:
			if json.loads(line).get("command") == "shutdown":
				break
		runtime.stop()

	threading.Thread(target=commands, daemon=True).start()
	runtime.run(lambda event: print(json.dumps(event), flush=True))
	print(json.dumps({"type": "stopped"}), flush=True)


def measure(args):
	key = uuid.uuid4().hex
	codec = runtimeModule.OpusCodec(args.channels, args.bitrate, args.frame_ms, encoder=False)
	session = transport.Session(threading.Event())
	process = None
	try:
		session.open(args.host, args.port, key, "subscriber", codec.payloadBytes)
		command = [sys._base_executable, str(Path(__file__).resolve()), "--child"]
		command += [
			f"--host={args.host}",
			f"--port={args.port}",
			f"--key={key}",
			f"--sources={args.sources}",
			f"--bitrate={args.bitrate}",
			f"--channels={args.channels}",
			f"--frame-ms={args.frame_ms}",
		]
		process = subprocess.Popen(
			command,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			encoding="utf-8",
			creationflags=subprocess.CREATE_NO_WINDOW,
			env=dict(os.environ, PYTHONPATH=sysconfig.get_path("purelib")),
		)
		events = queue.Queue()

		def readEvents():
			for line in process.stdout:
				try:
					events.put(json.loads(line))
				except ValueError:
					events.put({"type": "diagnostic", "message": line.strip()})

		reader = threading.Thread(target=readEvents, daemon=True)
		reader.start()
		deadline = time.monotonic() + 10
		while True:
			event = events.get(timeout=max(0.01, deadline - time.monotonic()))
			if event["type"] == "ready":
				break
			if event["type"] == "error":
				raise RuntimeError(event)
		started = time.monotonic()
		cpuStart = processCpu(process)
		arrivals, sequences, nonzero = [], [], 0
		payloadBytes = networkBytes = 0
		while time.monotonic() - started < args.seconds:
			packet = session.poll(0.05)
			parsed = transport.parseAudio(packet, session.identity, codec.payloadBytes) if packet else None
			if parsed:
				sequence, payload = parsed
				pcm = codec.decode(payload)
				payloadBytes += len(payload)
				networkBytes += len(packet) + (28 if session.udp.family == socket.AF_INET else 48)
				arrivals.append(time.monotonic())
				sequences.append(sequence)
				nonzero += bool(pcm.strip(b"\0"))
		elapsed = time.monotonic() - started
		cpu = processCpu(process) - cpuStart
		gaps = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
		missing = sum(max(0, b - a - 1) for a, b in zip(sequences, sequences[1:]))
		result = {
			"bitrate_kbps": args.bitrate,
			"channels": args.channels,
			"frame_ms": args.frame_ms,
			"payload_kbps": round(payloadBytes * 8 / elapsed / 1000, 2),
			"network_kbps": round(networkBytes * 8 / elapsed / 1000, 2),
			"sources": args.sources,
			"seconds": round(elapsed, 2),
			"cpu_percent_one_core": round(cpu / elapsed * 100, 2),
			"packets": len(arrivals),
			"missing": missing,
			"nonzero_packets": nonzero,
			"gap_p95_ms": round(statistics.quantiles(gaps, n=100)[94], 2) if len(gaps) > 1 else None,
			"gap_max_ms": round(max(gaps), 2) if gaps else None,
		}
		result["process_exit"] = process.poll()
		result["events"] = list(events.queue)
		return result
	finally:
		if process is not None:
			try:
				process.stdin.write('{"command":"shutdown"}\n')
				process.stdin.flush()
				process.wait(timeout=3)
			except (OSError, subprocess.TimeoutExpired):
				process.kill()
				process.wait()
			process.stdin.close()
			reader.join(1)
			process.stdout.close()
		session.close()
		codec.close()


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--host", required=True)
	parser.add_argument("--port", type=int, default=6838)
	parser.add_argument("--seconds", type=float, default=10)
	parser.add_argument("--sources", type=int, choices=(1, 2, 3), default=3)
	parser.add_argument("--bitrate", type=int, choices=(64, 96, 192), default=96)
	parser.add_argument("--channels", type=int, choices=(1, 2), default=2)
	parser.add_argument("--frame-ms", type=int, choices=(10, 20), default=10)
	parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
	parser.add_argument("--key", help=argparse.SUPPRESS)
	args = parser.parse_args()
	if args.child:
		child(args)
		return
	print(json.dumps(measure(args)), flush=True)


if __name__ == "__main__":
	main()
