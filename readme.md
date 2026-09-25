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

## Low-latency Audio

Remote++ can relay remote audio through [NVDARemoteAudioServer](https://github.com/haitun001/NVDARemoteAudioServer). Deploy it on the host running the Remote Access server and expose TCP and UDP port `6838`. Remote++ reuses the server address and key from the current Remote connection.

Audio is off by default. The controller can enable it from the Remote Access menu. Both computers must run an audio-capable version of Remote++ and connect through NVDA's built-in Remote Access.

### Listen to remote system sounds

This option sends sound from the controlled computer's selected system sound device to the controller. The Windows default output is selected initially.

When the selected device also captures NVDA speech, Remote++ pauses the controlled computer's normal NVDA Remote speech feedback so that the same speech is not played twice. Speech feedback resumes when listening stops, audio is interrupted, or no valid audio is received.

### Voice call

A voice call provides two independent audio directions:

* The controlled computer's microphone audio is sent to the controller.
* The controller's microphone audio is sent to the controlled computer.

Each computer plays the other computer's microphone audio through its default output device. System audio and voice call can be enabled together, and each audio category has its own settings.

Each computer can select its own microphone in **NVDA Settings -> Remote++**. The controlled computer can also select which local output device to share. If a selected device becomes unavailable, capture switches to the Windows default device. If the default device changes during an active stream, enable the audio feature again.

### Audio settings

In **NVDA Settings -> Remote++**, system audio and voice call each have a separate set of settings:

* **Playback buffer**: Minimum buffering (default), 10, 20, 40, or 80 ms. More buffering can reduce interruptions caused by uneven packet arrivals, at the cost of playback delay.
* **Audio bitrate**: 64, 96 (default), or 192 kbps per stream. Lower bitrates use less network bandwidth but reduce detail; the approximate audio data rates are 8, 12, and 24 KB/s before packet overhead.
* **Audio channels**: Mono or Stereo (default). Mono can improve clarity at a low bitrate but does not halve traffic at the same bitrate.
* **Transmission mode**: Low latency, 10 ms per packet (default), or Balanced, 20 ms per packet. Balanced mode reduces packet overhead but adds waiting time.

Audio is encoded with Opus at a fixed 48 kHz sample rate. The default configuration is stereo, 96 kbps, and 10 ms packets. Minimum buffering does not mean zero latency: capture, packet collection, encoding, the network, and playback all add delay.

The controller chooses transmission settings; each computer chooses its own capture devices. These preferences apply to all connections, are stored in the connection manager preferences, and are independent of NVDA configuration. Saving or applying changes briefly restarts active audio.

TeleNVDA and older NVDA Remote connections do not expose these controls. An older Remote++ version can continue normal Remote control but cannot stream audio. One controller can own audio at a time, and the Remote channel can contain only one controlled computer. If another controlled computer joins, audio stops. The audio protocol is not encrypted; use a trusted network or VPN.

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

All features are also accessible from the NVDA Remote Access menu:

* Connection Manager...
* Swap Control Mode
* Listen to remote system sounds
* Voice call
* Connect to Default Server (only shown when auto-connect is configured)

## Requirements

* NVDA 2026.1 or later with built-in Remote Access enabled
* A running `NVDARemoteAudioServer` instance on the same host as Remote Access, listening on port `6838`
* Remote++ on both computers
* Windows; audio runs inside NVDA using its bundled comtypes, pycaw, and WavePlayer, plus the add-on's x64 Opus library. No separate runtime installation is required.

## Security

To ensure maximum security and prevent unintended access, this add-on is disabled on the secure desktop (e.g., UAC prompts, Windows login screen).

## Author

Cary-rowen <manchen_0528@outlook.com>
