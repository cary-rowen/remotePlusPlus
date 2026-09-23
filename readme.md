# Remote PlusPlus

Enhances NVDA Remote with productivity features for power users.

## Features

### Connection Manager

Quickly save, organize, and connect to frequently used remote sessions.

* Organize connections into groups
* Search connections by name or host
* One-click connect from saved list
* Copy connection link to clipboard

### Swap Control Mode

Instantly switch between controlling another computer and being controlled, without disconnecting.

### Connect to Default Server

Quickly connect to your configured auto-connect server with a gesture.

### Low-latency Audio Relay

Listen to system audio from the controlled computer or start a bidirectional voice call through
`NVDARemoteAudioServer`. The audio server uses the current Remote Access host
automatically and always connects on port `6838`; the existing Remote Access
key is reused. Audio is off whenever a Remote Access connection is created.

### Audio Settings

In **NVDA Settings -> Remote++**, the controlling computer can choose separate values for system audio and voice calls:

* **Playback buffer**: Minimum buffering (default), 10, 20, 40, or 80 ms.
  More buffering can reduce interruptions from uneven packet arrivals, at the
  cost of delayed playback. This is not the total end-to-end latency.
* **Audio bitrate**: 64, 96 (default), or 192 kbps for each stream. Lower
  bitrate reduces traffic at the cost of detail. These choices use approximately
  8, 12, or 24 KB/s per stream; packet headers add network traffic.
* **Audio channels**: Mono or Stereo (default). Mono can improve clarity at a
  low bitrate, but does not halve traffic at the same bitrate.
* **Transmission mode**: Low latency, 10 ms per packet (default), or Balanced,
  20 ms per packet. Balanced mode reduces packet overhead but adds waiting time.

The sample rate is fixed at 48 kHz. Minimum buffering does not mean zero
latency: capture, packet collection, encoding, the network and playback all add
delay. Audio arrives in whole packets even when a smaller playback buffer is selected.

These preferences are global across connections, including connections opened
from a link. They are saved alongside the connection manager's preferences and
are independent of NVDA configuration profiles. The controlled computer's own
preferences do not override the controller's request.

Apply or OK saves changes and briefly restarts active audio, keeping the
selected system-audio and voice-call features. Changing a choice alone or cancelling does
not apply it; Cancel after Apply does not undo an already applied change.
Audio stays off when it was off. Changes made during a pending audio request
are applied after that request succeeds; failures do not trigger automatic retries.
Remote Access itself stays connected. Invalid saved values fall back to defaults.

Both computers must use a Remote++ version supporting Opus. There is no PCM
fallback; an older peer cannot stream audio, while normal Remote Access control
and speech remain available. The two categories keep their own playback buffer,
bitrate, channel and transmission mode settings.
Both computers must connect through NVDA's built-in Remote Access. A connection
made through TeleNVDA or the older NVDA Remote add-on does not reach Remote++'s
audio controls, even when Remote++ is installed on both computers. If an audio
request times out, check which Remote client is connected on the controlled computer.
One controller owns audio at a
time: other controllers must wait until it is turned off or its owner disconnects.
Audio requires a single controlled computer in the Remote channel. If a second
controlled computer joins, system audio and voice calls stop automatically.

System audio captures the Windows default output. Remote++ suppresses the audio
publisher's Remote speech replay only while receiving audio and after confirming
NVDA uses this output with an audible speech synthesizer. Selecting No speech,
setting speech volume to zero, selecting another output device, or a peer that
cannot confirm this preserves Remote speech. Volume changes apply to each utterance.
Other controlled computers' speech and tones always retain their normal replay.
If no valid audio arrives for about half a second, Remote speech resumes while
the listener waits for audio to return; normal silent periods do not close audio
or repeatedly announce errors. Closing audio, a capture failure or reloading the
controlled computer's add-ons also restores Remote speech automatically.
Remote Access mute, including automatic mute on local control, also mutes the
audio listener. Unmuting resumes live audio without replaying the muted backlog.

## Connection Manager

The Connection Manager provides a convenient interface for managing your remote connections.

### List Shortcuts

| Action | Shortcut |
|--------|----------|
| Connect with reversed mode | `Shift+Enter` |
| Move connection up | `Alt+Up` |
| Move connection down | `Alt+Down` |
| Edit connection | `F2` |
| Delete connection | `Delete` |
| Select all | `Ctrl+A` |
| Copy link | `Ctrl+C` |

### Context Menu

Right-click on a connection to access additional options:

* Connect / Connect Reversed
* Edit
* Copy link
* Set as Auto-Connect
* Move Up / Move Down
* Delete

## Keyboard Shortcuts

| Command | Gesture |
|---------|---------|
| Open Connection Manager | `NVDA+Control+Shift+N` |
| Swap Control Mode | `NVDA+Control+Shift+W` |
| Connect to Default Server | None |

## Menu Items

All features are also accessible from the NVDA Remote menu:

* Connection Manager...
* Swap Control Mode
* Listen to remote system sounds
* Voice call
* Connect to Default Server (only shown when auto-connect is configured)

## Requirements

* NVDA 2026.1 or later with built-in Remote Access enabled
* A running `NVDARemoteAudioServer` instance on the same host as Remote Access,
  listening on port `6838`
* Remote++ on both computers
* Windows; audio runs inside NVDA using its bundled comtypes/pycaw and WavePlayer,
  plus the add-on's x64 libopus DLL. No separate runtime installation is required.

The audio relay encodes PCM16 capture as Opus, defaulting to 48 kHz stereo at
96 kbps with 10 ms packets. Decoded audio is played in 5 ms blocks. A voice
call uses two independent streams, one in each direction; system audio uses a
third stream when enabled.
Windows WASAPI converts capture to the negotiated sample rate and channels.
NVDA performs playback conversion through its existing Windows audio backend. The controlled computer can publish system sounds or
microphone input through independent streams. A voice call carries microphone
audio in both directions. Mono capture
is duplicated to stereo. Actual latency also depends on the device period and
the network; 10 ms packets do not guarantee 10 ms total latency.
Changing or disconnecting the default capture device stops audio with an error;
select the audio menu item again to use the new device. Capture queues are bounded
to 40 ms per source. Playback queues hold the selected buffer plus 40 ms, with at
most 20 ms additionally fed to NVDA. Stale audio is discarded on mute or media loss.
The audio protocol has no encryption; use a trusted network or VPN.

The two menu items are available only on the controlling computer. Enabling an
item sends a request to the controlled computer, so audio is not started until
one of the items is selected. If the other side is an older Remote++ client,
or does not have Remote++, the request times
out or reports that audio is unavailable while the Remote Access connection
continues normally. No audio shortcut is assigned by default.

## Security

To ensure maximum security and prevent unintended access, this add-on is disabled on the secure desktop (e.g., UAC prompts, Windows login screen).

## Author

Cary-rowen <manchen_0528@outlook.com>
