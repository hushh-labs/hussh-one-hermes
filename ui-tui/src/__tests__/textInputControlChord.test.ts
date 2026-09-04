import { describe, expect, it } from 'vitest'

import { isUnclaimedControlChord } from '../components/textInput.js'

/**
 * Reloading the dashboard tab typed a stray `l` into the prompt.
 *
 * The dashboard writes 0x0c into the PTY on every browser reattach so the TUI
 * repaints (`TUI_FORCE_REDRAW` in `hermes_cli/pty_session.py`). The parser
 * names a control byte after its letter, so that arrives as
 * `{name: 'l', ctrl: true}`. The chords this input claims (Ctrl+A/E/U/K and
 * friends) are handled earlier; Ctrl+L is claimed by nothing, so it fell
 * through to the insert path and was typed as a letter, once per reload.
 *
 * Verified end to end with Playwright against the live dashboard: before, one
 * reload turned `/hussh-one` into `/hussh-onel`; after, two reloads left it
 * unchanged.
 */

const key = (over: Partial<{ ctrl: boolean }> = {}) => ({ ctrl: false, ...over })

describe('isUnclaimedControlChord', () => {
  it('drops the redraw chord the dashboard sends on every reattach', () => {
    expect(isUnclaimedControlChord(key({ ctrl: true }), 'l')).toBe(true)
  })

  it('drops any single-letter Ctrl chord, since none of them is a character', () => {
    for (const letter of ['a', 'l', 'r', 'z']) {
      expect(isUnclaimedControlChord(key({ ctrl: true }), letter)).toBe(true)
    }
  })

  it('leaves ordinary typing alone', () => {
    expect(isUnclaimedControlChord(key(), 'l')).toBe(false)
    expect(isUnclaimedControlChord(key(), 'x')).toBe(false)
  })

  it('never swallows a paste, which is text by definition', () => {
    expect(isUnclaimedControlChord(key({ ctrl: true }), 'l', true)).toBe(false)
  })

  it('leaves multi-character input alone, so IME and paste bursts still insert', () => {
    expect(isUnclaimedControlChord(key({ ctrl: true }), 'hello')).toBe(false)
  })
})

// The other half of this contract — that 0x0c parses as {name: 'l', ctrl:
// true} — is pinned in the ink package, which owns the parser:
// packages/hermes-ink/src/ink/force-redraw-chord.test.ts
