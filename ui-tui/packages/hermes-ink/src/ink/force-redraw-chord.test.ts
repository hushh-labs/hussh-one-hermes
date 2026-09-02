import { describe, expect, it } from 'vitest'

import { INITIAL_STATE, parseMultipleKeypresses } from './parse-keypress.js'

/**
 * The byte the dashboard writes into the PTY on every browser reattach.
 *
 * `hermes_cli/pty_session.py` sends `TUI_FORCE_REDRAW = b"\x0c"` after
 * replaying the buffer so a reattached xterm never opens on a stale frame.
 * That byte is Ctrl+L, and the parser names a control byte after the letter it
 * corresponds to — so it arrives looking exactly like the letter `l` unless
 * the consumer honours `ctrl`. It did not, and reloading the tab typed an `l`
 * into the prompt once per reload.
 *
 * If this ever stops reporting `ctrl`, the guard in textInput
 * (`isUnclaimedControlChord`) silently stops matching and the letter starts
 * being typed again, so the shape is pinned here rather than assumed.
 */
describe('the force-redraw byte', () => {
  it('is a Ctrl chord, not a character', () => {
    const [keys] = parseMultipleKeypresses(INITIAL_STATE, '\x0c')

    expect(keys).toHaveLength(1)
    const parsed = keys[0] as { kind: string; name: string; ctrl: boolean }
    expect(parsed.kind).toBe('key')
    expect(parsed.name).toBe('l')
    expect(parsed.ctrl).toBe(true)
  })
})
