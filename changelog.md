## 1.0.0

- Raise the minimum supported NVDA version to 2026.1.
- Add low-latency audio relay for listening to the controlled computer's system sounds and making bidirectional voice calls. This requires [NVDARemoteAudioServer](https://github.com/haitun001/NVDARemoteAudioServer) to be deployed on the host running the Remote Access server with TCP and UDP port 6838 exposed; Remote++ reuses the server address and key from the current Remote connection.
- Add separate playback buffer, bitrate, channel, and transmission mode settings for system audio and voice calls in NVDA Settings → Remote++.
- Add "Listen to remote system sounds" and "Voice call" to the Remote Access menu.
- Pause the controlled computer's normal NVDA Remote speech feedback while remote system sound listening is enabled to avoid duplicate speech.
- Require an audio-capable Remote++ version on both computers; older versions continue to support normal Remote control.
- Add Turkish translation.
