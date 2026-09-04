/**
 * Reconnect policy for the ChatSidebar `/api/events` subscriber socket.
 *
 * Pure helpers, no DOM: the component owns the socket and the timer, this
 * module owns the arithmetic and the "is this close code worth retrying"
 * decision so both can be unit-tested without a fake WebSocket.
 */

export const EVENTS_RECONNECT_BASE_MS = 1_000;
export const EVENTS_RECONNECT_MAX_MS = 30_000;
export const EVENTS_MAX_RECONNECT_ATTEMPTS = 15;
/** Bound ticket minting plus the WebSocket opening handshake. */
export const EVENTS_CONNECT_TIMEOUT_MS = 15_000;

/** Normal closure — the server said goodbye, don't chase it. */
const WS_CLOSE_NORMAL = 1000;
/** Credential rejected: the ticket or session token was not accepted. */
const WS_CLOSE_TICKET_REJECTED = 4401;
/** Ticket rejected / forbidden: retrying just burns tickets, user must reload. */
const WS_CLOSE_AUTH_CODES = new Set([WS_CLOSE_TICKET_REJECTED, 4403]);

/**
 * Exponential backoff, 1s → 2s → 4s → … → 30s cap.
 *
 * `attempt` is 0-based: attempt 0 is the first retry after the initial
 * connection dropped.
 */
export function eventsReconnectDelayMs(attempt: number): number {
  const exponent = Math.max(0, Math.trunc(attempt));

  // 2 ** exponent overflows to Infinity long before it matters; Math.min
  // still clamps correctly, but guard anyway so the delay stays a number.
  const raw = EVENTS_RECONNECT_BASE_MS * 2 ** Math.min(exponent, 32);

  return Math.min(raw, EVENTS_RECONNECT_MAX_MS);
}

/**
 * Whether a close code should trigger a retry.
 *
 * Auth rejections are terminal (the banner tells the user to reload) and a
 * normal 1000 close is intentional. Everything else — gateway restart,
 * network drop, 1005/1006, proxy timeout — is worth retrying.
 */
export function shouldRetryEventsClose(code: number | undefined): boolean {
  if (code === undefined) {
    return true;
  }

  return code !== WS_CLOSE_NORMAL && !WS_CLOSE_AUTH_CODES.has(code);
}

export function isEventsAuthRejection(code: number | undefined): boolean {
  return code !== undefined && WS_CLOSE_AUTH_CODES.has(code);
}

/**
 * One retry allowance for a rejected credential, matching the PTY socket's
 * `PTY_MAX_AUTH_RETRIES` so the two sockets tell the user the same story.
 *
 * A gated-mode ticket is single-use with a 30s TTL, so the first rejection
 * can mean "late" rather than "dead", and the next attempt mints a fresh
 * one. A credential rejected twice in a row is dead: stop, and say so.
 */
export const EVENTS_MAX_AUTH_RETRIES = 1;

/**
 * The allowance covers 4401 only. 4403 is a Host/Origin refusal: the request
 * is at the wrong address, and no credential makes it right.
 *
 * @param authRejections Consecutive auth rejections including this one,
 *   reset to 0 whenever the socket opens.
 */
export function shouldRetryEventsAuthRejection(
  code: number | undefined,
  authRejections: number,
): boolean {
  return code === WS_CLOSE_TICKET_REJECTED && authRejections <= EVENTS_MAX_AUTH_RETRIES;
}

// The sidebar's banner is shared with `info.credential_warning` and with the
// JSON-RPC sidecar's errors, so the events socket may only clear a message it
// wrote itself. Everything this module can put in the banner is listed here.
export const EVENTS_DISCONNECTED_MESSAGE =
  "events feed disconnected — the chat title may not update";

export function eventsReconnectingMessage(delayMs: number): string {
  return `events feed disconnected — reconnecting in ${Math.round(delayMs / 1000)}s…`;
}

export function eventsRejectedMessage(code: number): string {
  return `events feed rejected (${code}) — reload the page`;
}

export function eventsGaveUpMessage(): string {
  return `events feed disconnected — gave up after ${EVENTS_MAX_RECONNECT_ATTEMPTS} attempts, reload the page`;
}

/**
 * True when `message` is one this module produced, i.e. safe to clear on a
 * successful reconnect. Guards against stomping a `credential_warning` or a
 * sidecar error that happens to be showing when the feed recovers.
 */
export function isEventsFeedMessage(message: string | null): boolean {
  if (!message) {
    return false;
  }

  return message.startsWith("events feed ");
}
