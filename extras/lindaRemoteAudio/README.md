# Linux system audio with Linda

This experimental companion lets a Windows Remote++ controller listen to a
Linux desktop running [Linda](https://github.com/Mudb0y/linda). The regular
Remote++ add-on uses Windows APIs and cannot load in Linda. Install this
companion on Linux and keep the regular Remote++ add-on on Windows.

The companion implements the version 3 Remote++ control envelope and publishes
the `system_audio` stream through NVDARemoteAudioServer. It reuses this
repository's Opus codec and TCP/UDP transport. It supports all twelve existing
bitrate/channel/frame combinations. It does not provide voice calls, microphone
capture, a connection manager, or audio playback on Linux.

## Requirements

- Linda with built-in Remote Access and its NVDA-compatible add-on loader.
- A running PulseAudio server or PipeWire with PulseAudio compatibility.
- `parec` on PATH (Debian/Ubuntu: `pulseaudio-utils`) and system `libopus.so.0`
  (Debian/Ubuntu: `libopus0`).
- Remote++ on the Windows controller using audio control protocol version 3.
- An NVDA Remote relay and NVDARemoteAudioServer on the same host. The default
  audio port is 6838 TCP and UDP; the Remote relay normally uses 6837 TCP.

## Build and install

Run from the repository root with Python 3.13:

```sh
python tools/build_linda_addon.py
```

Install `lindaRemoteAudio-0.1.0.nvda-addon` using Linda's add-on manager and
restart Linda. The package declares `platforms = linux`; it is separate from
the Windows package and contains no Windows DLLs.

Connect Linda as the controlled computer and Windows NVDA as the controller
using the same relay and channel key. On Windows, turn on **Listen to remote
system sounds** in the Remote Access menu. No additional configuration is
needed in the companion. The audio host and channel key come from Linda's
current follower connection, never from a hardcoded address or key.

The captured device is PulseAudio's `@DEFAULT_MONITOR@`. Ensure Linda's speech
and the applications you want to hear use that default sink. The response
declares that the stream includes speech, allowing Remote++ to suppress
duplicate Remote speech on Windows. Changing the default sink requires
turning remote audio off and on again.

## Lifecycle and privacy

Capture starts only after a request from a currently connected controller.
Another controller cannot replace or stop the current owner's stream. Capture
stops when the owner leaves, Remote disconnects, the add-on unloads, or audio
capture fails. Audio is not recorded to disk. Anyone authorized in the same
audio room may hear the published system sound, including notifications and
speech. Protect the channel key and restrict relay/audio server access to
trusted users, for example through a private network. The existing audio
transport does not encrypt media.

Requests are handled by one worker with a bounded queue. Capture reads are
cancellable, stalled capture fails, and the `parec` child is terminated and
reaped when publishing ends. A successful response is sent only after the
first audio frame has been captured and sent.

The implementation relies on Linda's NVDA-compatible private `_remoteClient`
follower session, transport callbacks, and controller list. Changes to those
interfaces may require updating the companion.

## Validation

On Linux with system libopus installed:

```sh
python -m unittest discover -s tests -p test_linux_audio.py -v
python tools/build_linda_addon.py
```

The tests cover settings validation, controller authorization, cancellable
capture, capture EOF, and all twelve real Opus encode/decode combinations.
They do not connect to an existing Remote channel or capture user audio.
Linux-only tests skip on Windows; the existing Windows test job still checks
the shared codec against the bundled DLL.

A predecessor of this companion was heard successfully with Windows Remote++
and Linda 2026.3.1. This packaged revision adds lifecycle and validation fixes;
that earlier listening result is not a listening test of this revision.
