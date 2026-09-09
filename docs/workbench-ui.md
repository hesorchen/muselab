# Workbench controls and browser diagnostics

> [简体中文](workbench-ui_zh.md)

## Settings drafts

Settings with a Save button remain drafts until submitted. Closing Settings or pressing Escape prompts when changes remain. Choose to keep the drafts, then reopen to continue editing. Opening Memory Center also preserves them.

Drafts stay in this page's memory. This feature never writes API keys into localStorage or sessionStorage. While unsaved edits exist, the browser is asked to warn before refresh, closing the tab or leaving the site; browser restrictions may limit that warning, so save before leaving. The existing file-editor warning remains active.

Conversation defaults and API keys use the footer Save button. Provider editors, MCP, Hooks and the memory engine retain their own Save or Add actions. Saving one section does not submit unrelated configuration. For defaults, API keys, provider edits and memory engine configuration, changes made while their save request is pending remain unsaved drafts.

## Interface preferences

Desktop browsers and the mobile PWA access the same working files and session state. The interface supports light, dark, and eye-care modes, a custom accent color, and English/Chinese switching without a refresh. Some layout and display preferences stay in the current browser rather than syncing across devices. See [Mobile PWA](mobile.md) for installation and notification setup.

## File and conversation controls

More tools in the conversation header contains Task delivery and environment, Skills, MCP, image generation, scheduled tasks and Reload. The command palette retains its shortcut and search entry.

The empty preview offers search, creation and upload actions. On desktop, Hide preview focuses the conversation. Existing layout preferences are preserved across startup.

- **File management**: Drag-and-drop uploads, progress indicators, fuzzy search, rename, and trash. File references retain their source workspace; switching workspaces does not change their targets.
- **Editing and previewing**: Markdown editing and split preview, find-in-preview, zoom, reading-position restoration, and retained live HTML page state.
- **Global search**: Search file names, file contents, sessions, message history, and common actions from one place.
- **Real terminals**: Create, rename, and switch PTY terminals. Each remains bound to its original working directory. See [Terminal](terminal.md) for Profiles and mobile controls.

## Continuous session workflows

Session tabs support background execution. Switching tabs does not interrupt replies in other sessions. Messages submitted during execution can enter a persistent queue for ordered processing. See [UX reliability](ux-reliability.md) for submission receipts, deduplication, cancellation, and connection recovery.

- **Session forks**: Use “Fork from here” to retain history through the selected point and continue in a new session; the source remains unchanged.
- **Last-turn retries**: Use “Retry the last turn in a new branch” in the turn controls to resend the last request in a new branch rather than overwrite the original reply. Wait for current and background tasks to finish. Turns with attachments require reattaching the files before sending.

Workspace switching changes the current view and the default directory for new sessions. Existing sessions and terminals retain their own working directories. See [Workspace instructions](personalize-claude-md.md) for context and directory configuration.

## Global task management

The global task center shows task states across sessions, including running, failed, and completed tasks awaiting review. Background tasks, Monitor, and activity records help track progress and results; return to the owning session for the full execution history.

See [Scheduled tasks](scheduler.md) for scheduled execution. With Web Push configured, task completion can trigger notifications; setup is covered in [Mobile PWA](mobile.md).

## Execution records and configuration

Sessions display the thinking blocks, tool calls and results, code diffs, nested subagent timelines, and Hook status supplied by the runtime. Context usage, per-model usage, and turn duration help inspect each turn. Available records depend on the selected model and runtime.

Settings provides model and Hook configuration, with separate entry points for memory management and service maintenance. See [Providers](providers.md) for model setup, and [Skills](skills.md) and [Configuration](configuration.md) for extensions.

## Annotating HTML elements

1. Open an HTML file from the current workspace.
2. Click Annotate an element in the preview toolbar.
3. Click an element in the page; its highlight shows the selection.
4. Add a short comment and choose Add to conversation.

The quote retains the source workspace and absolute file path, CSS locator, visible text and comment. Adding it does not send a message or modify a file; you decide when to send. Switching files or workspaces ends the annotation.

Annotation works only in HTML file previews where MuseLab supplies its bridge script. It never injects code into arbitrary external pages and does not support browser-owned PDF viewers. HTML above the bridge injection size limit reports annotation as unavailable. The iframe keeps its isolated sandbox origin. Annotation does not read form values or send file paths or authentication tokens into the iframe. Locators describe the current page structure and may become outdated after edits.

Existing text-selection quotes and independent side questions remain separate.

## Memory sources

Memory Center provides confirmation, correction, forgetting and source controls. Retrieval counts mean a reference was offered to a task, not that the model used it. With automatic memory off, you can still explicitly save records; retrieval and learning require engine configuration. Implementation and data-service details live under How memory works.

## Browser responsiveness

Settings → Service & diagnostics → Browser responsiveness shows first contentful paint, workspace readiness, long tasks and first/final visible response timings for the latest 12 measured turns.

The observer loads during browser idle time and the panel reports its actual start. Visibility is counted only for the current conversation, a visible document and the matching message node inside the viewport. Background conversations, unpainted results and turns before monitoring began remain unmeasured rather than appearing as zero. Turn timing begins when this page starts establishing that turn's stream, excluding time previously spent in the persistent queue.

Only numeric diagnostics remain in this page's memory; conversation text, file paths and request URLs are not uploaded. Refresh resets them. Device, tab visibility and network conditions affect results; these are not direct measurements of model latency.

Stream queue status comes from the service and shows depth, estimated bytes, oldest-event wait and overflows since service startup. Older services explicitly report unavailable metrics.

Codex quota refresh reads the account authenticated on the MuseLab host. Sign in with `codex login` on that host if authentication is missing or expired. A Gateway login does not establish a local Codex login. Refresh failures stay visible; historical snapshots retain their original timestamp and are marked as historical, never as current remaining quota. The refresh button waits for both usage and quota requests.

Small popups (More tools, session history, open file tabs and context breakdown) follow their trigger as the pane or visible viewport changes. They stay inside the pane and above mobile navigation, flip when space is limited, and scroll when necessary. Pulling down on the file tree refreshes it using the existing refresh-button spinner; no separate status strip is displayed.
