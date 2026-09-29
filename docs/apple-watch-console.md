# Apple Watch and iPhone Shortcuts

MuseLab supports Apple Watch through one Shortcut named **MuseLab**. Dictate a message, browse recent sessions and replies, and send follow-up messages in the same session. The same Shortcut also runs on iPhone.

## Install and configure

1. Upgrade your MuseLab instance to **v2.1.0 or later**. Prepare an HTTPS address reachable from your phone and watch, and the instance's MuseLab token.
2. Open the [MuseLab v2.1.0 release](https://github.com/hesorchen/muselab/releases/tag/v2.1.0) in Safari on iPhone. Download the signed `MuseLab-v5.shortcut`, open it, and add the Shortcut.
3. Answer the import questions with your server address and token. An example address is `https://muselab.example`, without a trailing `/`. If import questions do not appear, edit the first two Text actions, labeled “服务器地址” and “MuseLab token”. Keep the client UUID unchanged.
4. Run once on iPhone, allow access to your server, and verify that the menu appears.
5. Enable **Show on Apple Watch** in the Shortcut details. After iCloud sync, run it from the watch's Shortcuts app.

The release template contains placeholders only. Downloading and signing do not require your real server token; enter it only after importing on your own devices.

## Menu and follow-up messages

| Action | Behavior |
| --- | --- |
| ➕ 新建并发送 — New session and send | First menu item. Creates a session after input and confirmation; cancelling or empty input creates no session |
| 🎙️ 继续上次会话 — Continue last session | Sends to the last session used by this Shortcut; falls back to recent sessions when none is saved |
| 🗂️ 最近会话 — Recent sessions | The 10 most recently updated sessions in the default workspace, without search or list pagination |
| 📖 查看最近回复 — Latest reply | Shows the last session's recent text reply, timestamp, and current state |

A session menu provides message input, latest reply, the last six text messages, queued-message management, and stopping the active turn. The current Shortcut menu labels are Chinese; the browser/PWA bilingual interface and themes are separate features.

Use the system microphone in the watch input screen to dictate. Confirming a send shows a short acknowledgement and returns to the same session menu. After reading, choose **继续对话** to send a follow-up. **刷新回复** refreshes the latest existing output. Long content is displayed in chunks with a continue-reading action.

The acknowledgement means accepted and queued, not completed. A recent reply can belong to an earlier turn. Handle tool permission requests in the MuseLab web interface.

Withdrawing a queued message and stopping a turn both require confirmation. Stop actions target a specific turn and keep pending messages. You can return or exit at each menu; cancelling system input, selection, or confirmation ends the run. A run supports up to 40 menu interactions; reopen the Shortcut afterward.

iPhone and Watch share the client UUID and saved last-session context, which persists privately on the server across restarts. Use different client UUIDs if separate Shortcut copies should remember different sessions.

## Troubleshooting

- **No menu or unreadable response:** check the HTTPS address, token, and network permission. Use your instance's root address, not the release page or download link. The template extracts response text, parses a dictionary, and then reads interface fields; web pages and non-menu responses show a connection message.
- **Expired menu:** return home and select again. Actions expire and are scoped to the client and workspace.
- **Reply unchanged after sending:** the request may be queued or running. Check session state and refresh manually; this entry point does not poll in the background.
- **Shortcut missing on Watch:** check Show on Apple Watch and iCloud Shortcuts sync for the paired account; first verify the run on iPhone.
- **Modified template cannot be imported:** rebuild and sign it again. Published files are already signed; users do not need to sign on a Mac.

Menus and input use system Shortcuts components. Custom chat layouts and animations require a native watchOS app. Automated checks cover the protocol, queue behavior, and generated-workflow data flow; verify device import, permissions, dictation, and sync during installation.

## Development and versions

```bash
python3 scripts/build_watch_console.py --output /tmp/muselab-watch
```

This creates `MuseLab.unsigned.shortcut` and `MuseLab.xml.plist`, without real credentials. Modified templates need signing before distribution. The v5 release was signed by RoutineHub HubSign using the placeholder template; signature, certificate chain, variable references, and equality with source action parameters were checked before publication.

Source and tests live in the MuseLab repository; signed files are Release assets. Template version v5 and menu protocol version v2 are managed separately.

`GET /api/watch/menu?client_id=<UUID>` returns the home menu; `POST /api/watch/actions` dispatches actions. Both require `X-Auth-Token` and return `Cache-Control: no-store`. View kinds are `menu`, `input`, `confirm`, `message`, and `exit`. Actions are signed, expire, and bind to the client and workspace; input is confirmed before submission.

Base adapters expose recent sessions, message submission, submission receipts, and latest replies. Admission uses MuseLab's durable queue and `request_id` deduplication. A dropped client connection does not cancel a submission already accepted by the server. See `backend/api_watch.py`, `backend/api_watch_console.py`, `tests/test_api_watch.py`, `tests/test_watch_console.py`, and `tests/test_watch_shortcut.py`.
