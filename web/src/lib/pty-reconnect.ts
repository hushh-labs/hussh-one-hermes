export type PtyConnectionState =
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed"
  | "ended";

export const PTY_RECONNECT_INPUT_MESSAGE =
  "Chat is reconnecting. Input will resume when connected.";

// Minimum gap (ms) between page-resume-triggered reconnect attempts, so a
// burst of visibilitychange/pageshow/focus/online events on tab-return
// collapses into a single reconnect.
export const PTY_RESUME_RECONNECT_THROTTLE_MS = 1000;

// If a socket sits in WS_CONNECTING past this budget it is treated as wedged
// (e.g. a half-open mobile socket after a radio handoff — the NS-591 case)
// and force-closed so `onclose` → scheduleReconnect can recover it.
export const PTY_CONNECTING_TIMEOUT_MS = 8000;

// The same budget for the phase *before* the socket exists: in gated mode a
// connect first awaits a fresh single-use ticket. That request produces no
// WebSocket, so a rejection or a hang is invisible to both `onclose` and the
// CONNECTING timer above — the tab would sit on "connecting" forever with no
// retry. Bound it so the failure routes into the ordinary backoff instead.
export const PTY_TICKET_TIMEOUT_MS = 8000;

// How long after a resumed socket opens we keep suppressing ANSI erase codes
// (`ESC[K` / `ESC[X`) from the PTY stream. Ink's two-pass virtual scroll emits
// them while replaying a long session; past that replay they are legitimate
// in-place redraws (spinners, progress bars, status lines) and must reach
// xterm or stale glyphs are left on screen. The replay of a 200+ message
// session takes ~10-20s, so this is deliberately generous — over-running the
// window only costs a few stale cells on a buffer that is about to be
// repainted, while under-running it re-opens the blank-viewport bug.
export const PTY_RESUME_SANITIZE_WINDOW_MS = 30000;

// Application close code the gateway sends when the credential presented on
// the upgrade is rejected (`web_server.pty_ws` -> `_ws_reject_after_accept`).
// The usual cause is mundane and invisible: the dashboard session token is
// minted fresh on every gateway start (`_resolve_session_token`), so every tab
// opened before the last restart holds a dead credential. Retrying cannot mint
// a new one, only a reload can, so this close is terminal by construction.
export const PTY_CLOSE_AUTH_REJECTED = 4401;

// Abnormal closure: the socket died with no close frame. Ambiguous on its own,
// a dropped network and a REFUSED HANDSHAKE both land here, which is exactly
// why the server answers with 4401 instead of leaving the client to guess.
export const PTY_CLOSE_ABNORMAL = 1006;

// Fallback for a gateway too old to send 4401. Such a server refuses the
// upgrade before accepting it, which fails the HTTP handshake and reaches the
// browser as a bare 1006, indistinguishable from signal loss. A real network
// drop eventually reconnects; a refused handshake never will. So bound the
// automatic retries by how many consecutive attempts never reached `onopen`
// and hand the tab back to the user rather than spinning forever.
//
// Giving up must NOT be terminal: the page-resume path
// (`shouldReconnectPtyOnPageResume`) still fires on visibilitychange /
// focus / online from the `closed` state, so a genuine outage recovers by
// itself the moment connectivity returns.
export const PTY_MAX_UNOPENED_ATTEMPTS = 8;

// One 4401 does not always mean "dead". In gated mode the credential is a
// single-use ticket with a 30s TTL, so a rejection can mean "late", and the
// next attempt mints a fresh one. Give the credential exactly one more
// chance; rejected twice in a row it is dead, not late. (The loopback
// session token is dead on the first rejection either way, so this costs a
// single ~250ms retry there and changes no outcome.)
export const PTY_MAX_AUTH_RETRIES = 1;

export const PTY_STALE_TAB_MESSAGE =
  "This tab's chat session expired (the agent gateway restarted). Reload to reconnect.";

export function isPtyAuthRejection(code: number | null | undefined): boolean {
  return code === PTY_CLOSE_AUTH_REJECTED;
}

export interface PtyRetryCloseInput {
  /** Close code, or null when the attempt died before a socket existed. */
  code: number | null;
  /**
   * Consecutive connect attempts, including the one that just failed, that
   * never reached `onopen`. Reset to 0 by a successful open.
   */
  unopenedAttempts: number;
  /**
   * Consecutive credential rejections, including this one. Only consulted
   * when `code` is the auth-rejected code.
   */
  authRejections: number;
}

/**
 * Whether a failed PTY attempt is worth retrying automatically.
 *
 * Two reasons to stop, in order of honesty:
 *   1. the server said so (4401), and a fresh credential was already tried:
 *      further retries are a lie dressed as progress;
 *   2. nothing has opened in `PTY_MAX_UNOPENED_ATTEMPTS` tries: we cannot
 *      tell refusal from outage on a bare 1006, so stop guessing and let the
 *      user decide (reload) while page-resume keeps watching for the network.
 *
 * Everything else, including a genuine 1006 after a working session, keeps
 * the bounded backoff that recovers a half-open socket (NS-591).
 */
export function shouldRetryPtyClose({
  code,
  unopenedAttempts,
  authRejections,
}: PtyRetryCloseInput): boolean {
  if (isPtyAuthRejection(code)) {
    return authRejections <= PTY_MAX_AUTH_RETRIES;
  }
  return unopenedAttempts < PTY_MAX_UNOPENED_ATTEMPTS;
}

export interface PtyResumeReconnectInput {
  isActive: boolean;
  visibilityState?: DocumentVisibilityState;
  online: boolean;
  socketReadyState?: number | null;
  ptyState: PtyConnectionState;
  connectInFlight?: boolean;
}

const WS_CONNECTING = 0;
const WS_OPEN = 1;
const WS_CLOSING = 2;
const WS_CLOSED = 3;

export function shouldReconnectPtyOnPageResume({
  isActive,
  visibilityState,
  online,
  socketReadyState,
  ptyState,
  connectInFlight,
}: PtyResumeReconnectInput): boolean {
  if (!isActive || !online || visibilityState === "hidden") {
    return false;
  }
  if (ptyState === "ended") {
    return false;
  }
  if (socketReadyState === WS_OPEN) {
    return false;
  }
  // A connect is mid-flight (the async socket-open IIFE is awaiting its
  // ticket URL and hasn't assigned wsRef yet, or the socket is still
  // CONNECTING on a non-stuck attempt). Don't fire a redundant reconnect
  // into that window unless the tab already believes it is reconnecting or
  // closed and needs a fresh attempt.
  if (
    (connectInFlight || socketReadyState === WS_CONNECTING) &&
    ptyState !== "reconnecting" &&
    ptyState !== "closed"
  ) {
    return false;
  }
  return (
    socketReadyState === null ||
    socketReadyState === undefined ||
    socketReadyState === WS_CLOSING ||
    socketReadyState === WS_CLOSED ||
    ptyState === "reconnecting" ||
    ptyState === "closed"
  );
}

export function shouldBlockPtyInput(ptyState: PtyConnectionState): boolean {
  return ptyState !== "open";
}
