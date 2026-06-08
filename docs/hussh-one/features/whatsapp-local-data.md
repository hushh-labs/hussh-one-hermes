# Feature — Local WhatsApp Data, History & Message Recovery

## What it does
Reads the owner's local macOS WhatsApp database and media cache to (a) retrieve message
history and attachments that predate the bridge, and (b) edit/recover already-sent agent
messages.

## Why it matters
WhatsApp E2EE means servers don't retain delivered history/media — a restarted bridge
can't pull old files on demand. Reading the local Catalyst SQLite store gives the agent
full context (PDFs, links, prior messages) and the ability to fix a sent message in place.

## How it works (paths)
- DB: `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`
- Media: `…/group.net.whatsapp.WhatsApp.shared/Message/Media/`
- Core Data timestamps → Unix: `ZMESSAGEDATE + 978307200`.
- Key tables: `ZWACHATSESSION` (chats/groups), `ZWAMESSAGE` (messages), `ZWAMEDIAITEM`
  (attachments), `ZWAPROFILEPUSHNAME` (sender names), `ZWAGROUPMEMBER` (group senders).

## Message edit / recovery
- Bridge endpoint: `POST /edit { chatId, messageId, message }` (Baileys edit).
- Look up the target message's `ZSTANZAID` in the local DB, then call `/edit`.
- **Pitfall:** editing replaces the entire text including the header — always re-include the
  full stacked brand header when editing.

## Config / prerequisites
- macOS WhatsApp Desktop (Catalyst) installed and signed in.
- Read access to the Group Container path.

## Privacy / Security
- All reads are local to the owner's machine. Capsule sessions cannot use these tools
  (`file`/`terminal` stripped) — local-data access is owner-plane only.

## Tests
- Covered operationally; DB-shape documented in the `whatsapp-gateway-customization` skill.

## Status
✅ Shipped.

## Future
- Helper to copy retrieved attachments into `~/Downloads` on request.
- Cross-platform (non-macOS) local store support.
