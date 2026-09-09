/**
 * PTY resume output sanitizer — strips pathological ANSI sequences that
 * Ink's two-pass virtual scroll emits during session resume.
 *
 * Three properties of the real pipeline drive this design:
 *
 * 1. **The PTY is in cooked mode (ONLCR).** `hermes_cli/pty_bridge.py` spawns
 *    via `ptyprocess.PtyProcess.spawn()` and never calls `setraw()`, so the
 *    line discipline rewrites every LF the child writes as CRLF. A burst
 *    therefore arrives as `\r\n\r\n\r\n…`, never as bare `\n\n\n…`, and any
 *    filter matching `\n{50,}` would never fire in production.
 *
 * 2. **Reads are chunked, not message-framed.** `bridge.read()` does
 *    `os.read(fd, 65536)` per drain tick and `pty_session.py` forwards each
 *    read as its own binary WebSocket frame. A CSI escape, a UTF-8 code
 *    point, *and* a long blank-line run can each straddle a frame boundary,
 *    so all three need trailing-state buffering to survive reassembly.
 *
 * 3. **Erase codes are only pathological during the resume replay.** Once the
 *    replay has settled, `ESC[K` / `ESC[X` are exactly how a TUI clears stale
 *    glyphs for spinners, progress bars, and status lines. Stripping them
 *    forever corrupts normal interactive output, so suppression is bounded to
 *    a short window after connect (see PTY_RESUME_SANITIZE_WINDOW_MS).
 */

/** A blank-line run: CRLF (real PTY, cooked mode) or bare LF (raw-mode PTY). */
const BLANK_LINE_BURST = /(?:\r?\n){50,}/g;
// eslint-disable-next-line no-control-regex -- intentional ESC byte in ANSI sequence parser
const ERASE_LINE = /\x1b\[\d*K/g;
// eslint-disable-next-line no-control-regex -- intentional ESC byte in ANSI sequence parser
const ERASE_CHAR = /\x1b\[\d*X/g;

 
const CSI_PARTIAL_BODY = /^[0-9:;<=>?]*[\x20-\x2f]*$/;

/**
 * Index of the start of a still-open escape sequence in `combined`, or -1
 * if none is open (everything is either plain text or fully terminated
 * sequences). Covers the two sequence families Ink's output actually uses:
 *
 * - **CSI** (`\x1b[...`): open while only param bytes (0x30-0x3F: digits
 *   plus `:;<=>?`) and/or intermediate bytes (0x20-0x2F) have arrived,
 *   with no final byte (0x40-0x7E) yet. See ui-tui's termio/csi.ts
 *   CSI_RANGE for the canonical byte ranges this mirrors (a separate
 *   package, not importable here).
 * - **OSC** (`\x1b]...`): open until a BEL (`\x07`) or ST (`\x1b\\`)
 *   terminator has arrived. Window/tab-title and default-color payloads
 *   are free-form text, so no character-class restriction applies to the
 *   body — only whether a terminator has shown up yet.
 *
 * `combined.lastIndexOf("\x1b")` alone isn't a sufficient way to find the
 * open sequence's start: an OSC terminated by ST (`\x1b\\`, two bytes)
 * contains its OWN embedded ESC as part of the terminator, so once
 * complete, "the last ESC in the buffer" points at that terminator, not
 * at the (already-resolved) opener — treating that position as the
 * pending start would wrongly re-buffer an already-complete sequence
 * forever (verified: `"a\x1b]0;Hermes\x1b\\b"` never released "b" without
 * checking each opener independently — `flush()` would eventually drop
 * the whole thing, corrupting output far worse than the original
 * split-frame bug this function exists to fix). So this checks each
 * opener on its own: is the last `\x1b[` still missing its final byte? Is
 * the last `\x1b]` still missing its terminator? Is the buffer's last
 * byte a bare, not-yet-classified `\x1b`? Ink's output is well-formed
 * (one sequence open at a time), so at most one of these is ever
 * genuinely open; if more than one somehow were, the earliest position is
 * returned — safe, since holding back more only delays emission, never
 * corrupts it.
 *
 * History: this used to be a single regex, `/^\x1b(?:\[[0-9:;<=>?]*[\x20-
 * \x2f]*)?$/`, tested only against the tail from `lastIndexOf("\x1b")`.
 * That missed two things:
 *
 * 1. DEC private-mode sequences (`CSI ? Pn h/l` — focus reporting 1004,
 *    mouse tracking, bracketed paste, cursor visibility) split right
 *    after the `?` (e.g. "\x1b[?100" | "4l").
 * 2. Every OSC sequence, full stop — window/tab title (`\x1b]0;...\x07`,
 *    `\x1b]2;...\x07`) and default-color paint/query (`\x1b]10/11;...`)
 *    are used constantly by Ink's output; a live capture of one session
 *    resume replay found 53 OSC sequences and confirmed synthetically
 *    that 10 of 11 possible split points inside a real one
 *    (`\x1b]0;Hermes\x07`) shipped a dangling fragment immediately.
 *
 * Either miss ships an incomplete sequence straight into `term.write()`
 * instead of holding it in `#pending`. If the socket then closes before
 * the rest arrives (a background tab's connection being force-closed, or
 * simply a page refresh landing mid-sequence), `flush()`'s partial-escape
 * drop (below) never triggers — the dangling fragment was already
 * written — leaving xterm's parser stuck "in-escape" or "in-OSC-string"
 * across the reconnect and misinterpreting whatever real output arrives
 * next, e.g. swallowing it as OSC payload until some later byte
 * (frequently a CSI final byte like `l`, 0x6c) happens to look like a
 * terminator.
 */
function findOpenEscapeStart(combined: string): number {
  let start = -1;

  const csiOpen = combined.lastIndexOf("\x1b[");
  if (csiOpen !== -1 && CSI_PARTIAL_BODY.test(combined.slice(csiOpen + 2))) {
    start = csiOpen;
  }

  const oscOpen = combined.lastIndexOf("\x1b]");
  if (oscOpen !== -1) {
    const afterOpener = combined.slice(oscOpen + 2);
    if (!afterOpener.includes("\x07") && !afterOpener.includes("\x1b\\")) {
      start = start === -1 ? oscOpen : Math.min(start, oscOpen);
    }
  }

  if (combined.endsWith("\x1b")) {
    const bareEsc = combined.length - 1;
    start = start === -1 ? bareEsc : Math.min(start, bareEsc);
  }

  return start;
}

/**
 * A trailing run of newlines that may continue into the next frame. Held back
 * so a burst split across frames still meets the collapse threshold instead of
 * slipping through as sub-threshold fragments. A lone trailing `\r` is
 * included: a frame boundary can fall between the CR and LF of a CRLF pair,
 * and emitting the CR early would break one run into two shorter ones.
 *
 * Always matches (possibly empty, at end of input).
 */
const TRAILING_NEWLINES = /(?:\r?\n)*\r?$/;

/** Collapsed form of a pathological burst: one blank row, CRLF for xterm. */
const COLLAPSED_BURST = "\r\n\r\n";

/**
 * Apply all suppression rules to a safely-completed string.
 *
 * @param stripErase When false, `ESC[K` / `ESC[X` are preserved. Blank-line
 *   burst collapsing still applies — a thousand-row burst is pathological
 *   whenever it appears, but erase codes are legitimate once resume settles.
 * Exported for focused unit-testing of filter behaviour.
 */
export function applyPtyFilters(input: string, stripErase = true): string {
  const collapsed = input.replace(BLANK_LINE_BURST, COLLAPSED_BURST);
  if (!stripErase) return collapsed;
  return collapsed.replace(ERASE_LINE, "").replace(ERASE_CHAR, "");
}

/** Stateful chunk processor that guards against cross-frame split sequences. */
export class PtyResumeSanitizer {
  #pending = "";
  #stripErase = true;

  /**
   * Stop stripping erase codes while continuing to collapse blank-line bursts.
   * Called when the resume-replay window closes so that ordinary interactive
   * redraws (spinners, progress bars, status lines) keep their `ESC[K`.
   */
  endEraseSuppression(): void {
    this.#stripErase = false;
  }

  /** True while erase-code stripping is still active. */
  get isSuppressingErase(): boolean {
    return this.#stripErase;
  }

  /** Feed one decoded WebSocket frame payload. Returns the sanitized output. */
  next(chunk: string): string {
    const combined = this.#pending + chunk;
    if (combined === "") {
      this.#pending = "";
      return "";
    }

    // Hold back a trailing partial escape so a CSI or OSC sequence split
    // across frames is still recognised once its terminator arrives.
    const openEscape = findOpenEscapeStart(combined);
    if (openEscape !== -1) {
      this.#pending = combined.slice(openEscape);
      return applyPtyFilters(combined.slice(0, openEscape), this.#stripErase);
    }

    // Hold back a trailing newline run so a burst spanning frames accumulates
    // to the collapse threshold instead of leaking through in fragments.
    // TRAILING_NEWLINES always matches; an empty match at end of input means
    // the frame does not end mid-run and nothing needs to be held back.
    const trailing = TRAILING_NEWLINES.exec(combined) as RegExpExecArray;
    if (trailing.index === 0) {
      // The whole frame is newlines — keep accumulating, emit nothing yet.
      this.#pending = combined;
      return "";
    }
    this.#pending = trailing[0];
    return applyPtyFilters(combined.slice(0, trailing.index), this.#stripErase);
  }

  /**
   * Drain buffered state at end of stream.
   *
   * A buffered *partial escape* is dropped rather than written: `#pending` only
   * ever holds an incomplete sequence, and emitting one would leave xterm's
   * parser in an "in-escape" state that swallows subsequent output after
   * reconnect. A buffered *newline run* is safe and is emitted (collapsed).
   */
  flush(): string {
    const last = this.#pending;
    this.#pending = "";
    if (last === "" || last.includes("\x1b")) return "";
    return applyPtyFilters(last, this.#stripErase);
  }
}
