# Feature — Social-Group Capsules (Sandboxed Containers)

## What it does
A **capsule** is a per-group sandbox that lets a non-owner social group (e.g. "Three
Musketeers") use the agent **without** access to the owner's private memory, profile,
work/credentials, or any ability to mutate state or message other chats. The capsule
grows its **own** isolated memory scoped only to that group.

## Why it matters
It lets the owner safely share the agent with friends/family/colleagues in a specific
group — fun, useful, conversational — with **zero** risk of leaking the owner's private
world. It's privacy-by-construction for shared contexts.

## How it works (modules)
- `gateway/whatsapp_capsule.py` — parses `whatsapp.capsules`, resolves a `CapsuleConfig`
  per chat JID, computes the read-only toolset + isolated memory dir.
- `gateway/run.py` — on a capsule session, activates context-local overrides:
  `set_memory_dir_override()` + `set_outbound_send_lock()`; injects the capsule system prompt.
- `tools/memory_tool.py` — memory-dir override → isolated MEMORY/USER vault.
- `tools/send_message_tool.py` — outbound send lock → cannot message other chats.
- `scripts/whatsapp-bridge/bridge.js` — `CAPSULE_GROUPS` lets **other members** trigger the
  agent in capsule groups (explicit tag only) + per-sender anti-DOS rate limit.

## Config schema (`config.yaml` → `whatsapp.capsules`)
```yaml
whatsapp:
  capsules:
    "120363405517552679@g.us":          # Three Musketeers JID
      name: "three-musketeers"
      memory_dir: "capsules/three-musketeers"
      skip_global_memory: true
      skip_global_user_profile: true
      enabled_toolsets: ["web", "vision"]
      disabled_toolsets: ["terminal","file","delegation","cronjob","skills","session_search","kanban","spotify","homeassistant","computer_use","messaging"]
      block_outbound_send: true
      system_prompt: >
        You are operating inside the Three Musketeers social group capsule...
```

## Bridge env knobs
- `WHATSAPP_CAPSULE_GROUPS` — JIDs where non-owners may tag `@One` (must also have a
  `whatsapp.capsules` entry for the sandbox to apply).
- `WHATSAPP_CAPSULE_RATE_MAX` — max non-owner invocations per window (**default 30**).
- `WHATSAPP_CAPSULE_RATE_WINDOW_MS` — sliding window ms (**default 60000** → 30/min/sender).

## Memory model
- Capsule builds and recalls memory **within** the group (running threads, preferences,
  inside jokes) — persisted under `HERMES_HOME/capsules/<name>/`.
- It **cannot** read the owner's global MEMORY/USER, and group memory **never** bleeds back
  into the owner's main agent. Two separate brains.

## Capsule contract (invariants)
1. **Memory isolation** — capsule resolves to its own dir; global MEMORY/USER not loaded.
2. **Read-only blast radius** — only `web`+`vision`; all mutating/sensitive tools stripped.
3. **No lateral send** — `block_outbound_send` forbids messaging any other chat.
4. **Non-owner triggering (capsule-only)** — others can invoke, but only via explicit tag;
   everywhere else stays owner-only.
5. **Anti-DOS rate limit** — non-owner invocations rate-limited pre-agent; owner never limited.
6. **Branding preserved** — canonical stacked header still applies.

## What others CAN / CANNOT ask
✅ `@One score of the match?` · `@One bar near Times Square` · `@One [photo] what dish is this?`
🚫 owner's number/address · owner's work files · "message X for me" · consent/MCP data · shell/files

## Tests
- `tests/gateway/test_whatsapp_capsule.py` — isolation, toolset, outbound refusal.
- `tests/gateway/test_whatsapp_group_gating.py` — triggering paths.

## Status
✅ Shipped.

## Future
- Per-capsule rate-limit overrides.
- Capsule analytics (usage per member) inside the capsule vault.
- One-command `hussh-one capsule add <jid> <name>` helper.
