import { describe, expect, it, vi } from 'vitest'

import App from './components/App.js'
import { INITIAL_STATE, parseMultipleKeypresses } from './parse-keypress.js'

/**
 * A stray "l" typed into the prompt after switching tabs.
 *
 * `l` is the final byte of every DEC private mode reset the terminal traffic
 * around a redraw is full of: hide cursor (`\x1b[?25l`), leave alt screen
 * (`\x1b[?1049l`), disable bracketed paste (`\x1b[?2004l`), disable focus
 * reporting (`\x1b[?1004l`). None of those is a keystroke.
 *
 * An escape sequence does not arrive atomically. The tokenizer buffers an
 * incomplete one until its final byte lands, and it survives every ordinary
 * delivery pattern: one read, byte at a time, any two-way split, and the 50ms
 * incomplete-escape watchdog flush. It did NOT survive a remount, because the
 * buffer lived on the App instance. A new App started from INITIAL_STATE, the
 * buffered prefix was gone, and the tail arrived as text with nothing to
 * complete -- so the terminal's own bookkeeping was typed into the focused
 * input, one character per redraw.
 *
 * The bytes belong to the stream, so the parse state does too.
 */

const noopStream = { isTTY: false, write: () => true } as unknown as NodeJS.WriteStream

const makeApp = (stdin: object) =>
  new App({
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: noopStream,
    stderr: noopStream,
    exitOnCtrlC: false,
    onExit: vi.fn(),
    terminalColumns: 80,
    terminalRows: 24,
    selection: undefined as any,
    onSelectionChange: vi.fn(),
    onClickAt: vi.fn(() => false),
    onMouseDownAt: vi.fn(() => undefined),
    onMouseUpAt: vi.fn(),
    onMouseDragAt: vi.fn(),
    onHoverAt: vi.fn(),
    onCopySelectionNoClear: vi.fn(async () => ''),
    getSelectedText: vi.fn(() => ''),
    getHyperlinkAt: vi.fn(() => undefined),
    onOpenHyperlink: vi.fn(),
    onMultiClick: vi.fn(),
    onSelectionDrag: vi.fn(),
    onStdinResume: vi.fn(),
    dispatchKeyboardEvent: vi.fn(),
    children: null as any
  } as any)

/** Every DEC private mode reset the terminal emits around a redraw. */
const REDRAW_SEQUENCES: Record<string, string> = {
  'hide cursor': '\x1b[?25l',
  'leave alt screen': '\x1b[?1049l',
  'disable bracketed paste': '\x1b[?2004l',
  'disable focus reporting': '\x1b[?1004l'
}

function printableKeys(keys: Array<Record<string, unknown>>): string {
  return keys
    .filter(k => k.kind === 'key' && typeof k.name === 'string' && /^[\x20-\x7e]$/.test(k.name as string))
    .map(k => k.name as string)
    .join('')
}

describe('escape sequence split across a remount', () => {
  for (const [label, sequence] of Object.entries(REDRAW_SEQUENCES)) {
    it(`${label} never reaches the input, at any split point`, () => {
      for (let cut = 1; cut < sequence.length; cut++) {
        const stdin = { id: 'shared-stdin' }
        const emitted: Array<Record<string, unknown>> = []

        // The App reading when the sequence starts arriving.
        const before = makeApp(stdin)

        const [firstKeys, firstState] = parseMultipleKeypresses(before.keyParseState, sequence.slice(0, cut))

        before.keyParseState = firstState
        emitted.push(...(firstKeys as Array<Record<string, unknown>>))

        // The redraw remounts. A different App instance, the same terminal.
        const after = makeApp(stdin)

        const [secondKeys, secondState] = parseMultipleKeypresses(after.keyParseState, sequence.slice(cut))

        after.keyParseState = secondState
        emitted.push(...(secondKeys as Array<Record<string, unknown>>))

        expect(printableKeys(emitted), `${label} split at ${cut}`).toBe('')
      }
    })
  }

  it('a remount inherits the buffered prefix rather than starting clean', () => {
    const stdin = { id: 'shared-stdin' }
    const before = makeApp(stdin)
    const [, state] = parseMultipleKeypresses(before.keyParseState, '\x1b[?204')
    before.keyParseState = state

    expect(before.keyParseState.incomplete).toBeTruthy()
    // The new App must see the same half-read sequence, not INITIAL_STATE.
    expect(makeApp(stdin).keyParseState).toBe(before.keyParseState)
  })

  it('a different terminal does not inherit another one’s half-read sequence', () => {
    const first = makeApp({ id: 'stdin-a' })
    const [, state] = parseMultipleKeypresses(first.keyParseState, '\x1b[?204')
    first.keyParseState = state

    expect(makeApp({ id: 'stdin-b' }).keyParseState).toBe(INITIAL_STATE)
  })
})
