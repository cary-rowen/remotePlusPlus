## 0.5.0

- Require NVDA 2026.1 or later.
- Add global playback buffer and transmission quality preferences in NVDA Settings → Remote++,
  with explicit default labels, live audio reconfiguration and legacy quality fallback.

- Prevent duplicate Remote++ items in the Remote Access menu.
- Show "Connect to Default Server" as soon as an auto-connect connection is set.
- Improve group handling and reordering in filtered connection lists.
- Improve dialog focus handling and local server detection.
- Add optional low-latency audio relay through NVDARemoteAudioServer.
- Add separate menu toggles for remote system sounds and bidirectional voice calls. The
  current Remote host is reused with the fixed audio port 6838; no audio
  shortcut or per-connection audio settings are added.
