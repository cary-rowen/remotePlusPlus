# Remote++

Adds productivity features to NVDA's built-in Remote Access for power users.

## Features

### Connection Manager

Quickly save, organize, and connect to frequently used remote sessions.

* Organize connections into groups
* Search connections by name or host
* One-click connect from saved list
* Copy connection link to clipboard

### Swap Control Mode

Instantly switch between controlling another computer and allowing this computer to be controlled, without disconnecting.

### Connect to Default Server

Quickly connect to your configured auto-connect server with a gesture.

## Low-latency Audio Relay

Remote++ can relay remote audio through [NVDARemoteAudioServer](https://github.com/haitun001/NVDARemoteAudioServer). Deploy it on the host running NVDA's built-in Remote Access server and expose TCP and UDP port `6838`. Remote++ reuses the server address and key from the current Remote Access connection.

Audio is off by default. The controlling computer can enable it from NVDA's built-in Remote Access menu. Both computers must run an audio-capable version of Remote++ and connect through NVDA's built-in Remote Access.

### Listen to remote system sounds

This option sends sound from the controlled computer's selected system sound device to the controlling computer. The controlled computer initially uses the Windows default output device.

When the selected device also carries NVDA speech, Remote++ pauses the controlled computer's normal Remote Access speech feedback so that the same speech is not played twice. Speech feedback resumes when listening stops, audio is interrupted, or no valid audio is received.

### Voice call

A voice call provides two independent audio directions:

* The controlled computer's microphone audio is sent to the controlling computer.
* The controlling computer's microphone audio is sent to the controlled computer.

Each computer plays the other computer's microphone audio through its default output device. System audio and voice call can be enabled together, and each audio category has its own settings.

Each computer can select its own microphone in **NVDA Settings -> Remote++**. The controlled computer can also select which local output device to share. If a selected device becomes unavailable, capture switches to the Windows default device. If the default device changes during an active stream, enable the audio feature again.

### Audio settings

In **NVDA Settings -> Remote++**, system audio and voice call each have a separate set of settings:

| Setting | Options |
| --- | --- |
| Playback buffer | Minimum buffering (default), 10, 20, 40, or 80 ms |
| Audio bitrate | 64, 96 (default), or 192 kbps |
| Audio channels | Mono or Stereo (default) |
| Transmission mode | Low latency: 10 ms per packet (default); Balanced: 20 ms per packet |

Audio is encoded with Opus at a fixed 48 kHz sample rate. The default configuration is stereo, 96 kbps, and 10 ms packets. Increasing the playback buffer can reduce interruptions caused by network jitter, but adds playback delay. Lower bitrates can reduce network traffic but reduce audio detail. Balanced mode can reduce packet count but adds waiting time.

The controlling computer chooses transmission settings; each computer chooses its own audio devices. These preferences apply to all connections, are stored in the connection manager preferences, and are independent of NVDA configuration. Saving or applying changes briefly restarts active audio.

TeleNVDA and older NVDA Remote connections do not expose these controls. An older Remote++ version can continue normal remote control but cannot stream audio. Only one controlling computer can use remote audio at a time, and when remote audio is in use, the Remote Access channel can contain only one controlled computer. If another controlled computer joins, audio stops. The audio protocol is not encrypted; use a trusted network or VPN.

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
| Connect to Previous Saved Connection | None |
| Connect to Next Saved Connection | None |
| Listen to remote system sounds | None |
| Voice call | None |

## Menu Items

The following features are also available from NVDA's built-in Remote Access menu:

* Connection Manager...
* Swap Control Mode
* Listen to remote system sounds
* Voice call
* Connect to Default Server (only shown when auto-connect is configured)

## Requirements

* NVDA 2026.1 or later with NVDA's built-in Remote Access enabled
* A running `NVDARemoteAudioServer` instance on the same host as NVDA's built-in Remote Access server, listening on port `6838`
* Remote++ on both computers
* Windows; audio runs inside NVDA using its bundled comtypes, pycaw, and WavePlayer, plus the add-on's x64 Opus library. No separate runtime installation is required.

## Security

To ensure maximum security and prevent unintended access, this add-on is disabled on the secure desktop (e.g., UAC prompts, Windows login screen).

## Author

Cary-rowen <manchen_0528@outlook.com>
