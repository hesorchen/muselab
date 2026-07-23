# Frontend internals

> [简体中文](frontend_zh.md)

This document explains muselab's browser architecture, state model, and
performance policy. See [Mobile](mobile.md) for PWA installation,
[Terminal](terminal.md) for the real terminal, and
[Model routing and the chat loop](routing.md) for the server-side stream.

## 1. No-build frontend

muselab uses plain HTML, CSS, JavaScript, and Alpine.js, with no Webpack, Vite,
or Rollup build step. During development, edit `frontend/` and refresh.

Only the base runtime loads at startup. highlight.js and KaTeX are warmed during
browser idle time, CodeMirror is warmed before editing interaction, and Mermaid
and xterm.js remain on demand. All third-party assets ship with the project; the
UI does not depend on a CDN.

Static asset URLs carry a version marker. The Service Worker is served from
`/sw.js` for whole-origin scope. The server caches rendered index HTML and
invalidates it when frontend assets change.

## 2. Workbench and state

Desktop uses a three-pane layout:

| Area | Main capabilities |
|---|---|
| Files | Multiple workspaces, tree, recent files, upload, search, trash |
| Preview | File tabs, real terminal tabs, preview, find, editing, Markdown split view |
| Chat | Conversation tabs, message stream, queue, attachments, model and permission controls |

Mobile reuses the same state and switches among Files, Preview, and Chat through
bottom navigation. The terminal shortcut in the chat header switches to Preview
and opens the full terminal manager.

Each conversation tab retains its own messages, draft, attachments, scroll
position, stream state, server-queue mirror, and footer state. Switching tabs
does not reset running work. Background tabs become unread when they finish.
The global activity center aggregates running, waiting, failed, and unread work
across sessions and workspaces.

## 3. File preview and editing

The preview pane supports Markdown, text, HTML, images, PDF, XLSX, and CSV.
HTML runs in a sandboxed iframe and cannot access the muselab page, browser
storage, or authentication data.

Markdown and text files can enter edit mode. Markdown provides edit, split, and
preview views and shows save state. In-file find supports hit navigation and
context results; global search locates content across files and conversations.

File and terminal tabs share the preview tab bar. A terminal process keeps
running when the user switches to a file preview.

## 4. Message rendering

The assistant Markdown pipeline is:

```text
streamed text → Markdown parse → HTML sanitization → final math and path enhancement
```

The streaming path skips expensive KaTeX and file-path traversal. The final
render runs when the segment or turn completes. Generated HTML artifacts and
HTML file previews run without `allow-same-origin`.

Desktop favors reading quality:

- Current segments below 32 KiB update about every 80 ms.
- Segments from 32–128 KiB update about every 160 ms.
- Longer segments update about every 320 ms.
- Four message panes may remain resident; normal open is 60 bubbles, with a
  300-node DOM cap and an 800-message memory cap.

Mobile favors power, memory, and WebView stability:

- Refresh intervals grow through 80, 160, 320, 600, 1,000, and 1,600 ms.
- Above 32 KiB, an in-flight segment temporarily uses plain text and receives
  one final rich render at completion.
- Only the active pane stays resident; normal open is 15 bubbles, with a
  60-node DOM cap and a 120-message memory cap.
- No background conversation preloading; older history loads 80 items at a
  time.

While the user selects text in a streaming message, HTML replacement pauses so
new tokens do not collapse the selection.

## 5. SSE conversation stream

A new turn connects in two steps:

1. The browser sends prompt, session, model, permission, and attachments to
   `POST /api/chat/stream/start`, authenticated by `X-Auth-Token`.
2. The server returns a short-lived one-time ticket. `EventSource` opens
   `/api/chat/stream` with only that ticket.

The prompt and long-lived token therefore stay out of the SSE URL, browser
history, and normal access logs. The legacy URL protocol is used only when an
older backend explicitly returns 404 or 405.

The client handles text, thinking, tool calls, background tasks, rate limits,
questions, permission requests, completion, cancellation, and resync events.
The server sends a heartbeat every 15 seconds. After 40 seconds of total
silence, the client uses an activity probe and disk replay to recover without
executing an already-completed turn again.

The server-side message queue is independent of the page connection. Users can
send while Muse is replying; queued messages run in order and may continue
after the page closes. Errors, stops, questions, and permission waits pause the
queue to prevent unattended cascading work.

## 6. Footer and context

The chat footer contains controls that directly affect the current turn:
attachments, model, permission, effort, and context usage. Elapsed time is
anchored to the server turn start, so reconnecting or switching tabs does not
reset it.

The context ring opens the SDK-reported usage breakdown and exposes native
compact. The UI warns near the context limit, but the provider and SDK remain
authoritative.

Skills, MCP, terminal, image generation, and the global activity center are
session- or workbench-level capabilities, so they live in the header rather
than crowding the mobile footer.

## 7. Terminal frontend

The terminal button in the preview header sits beside content search and opens
the terminal list and Profile selector. xterm.js loads on demand. Desktop keeps
10,000 scrollback lines; mobile keeps 3,000.

Mobile drives xterm scrollback with an explicit vertical touch gesture and
offers Copy, Paste, backslash, Ctrl+C, Esc, Tab, and arrow accessory keys.
See [Terminal](terminal.md) for lifecycle, Profiles, and security.

## 8. PWA and Service Worker

The Service Worker handles Web Push only; it does not cache pages or static
assets. A push is suppressed when any visible muselab window already exists.

When a backgrounded PWA returns, it compares asset versions and reloads if they
changed and no stream is active. This prevents stale JavaScript from talking to
a newer backend. See [Mobile](mobile.md) for installation, HTTPS, and push
limitations.

## 9. Internationalization and accessibility

The UI includes Simplified Chinese and English, initially chosen from the
browser language and overridable in Settings. Missing strings fall back to
Chinese and then to the key.

Controls keep keyboard and touch paths. Mobile inputs use at least 16 px text,
and the layout accounts for safe areas, reduced-motion preferences, and
on-screen keyboard viewport changes.
