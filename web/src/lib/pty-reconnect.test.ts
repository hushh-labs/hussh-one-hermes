import { describe, expect, it } from "vitest";

import {
  PTY_CLOSE_ABNORMAL,
  PTY_CLOSE_AUTH_REJECTED,
  PTY_MAX_AUTH_RETRIES,
  PTY_MAX_UNOPENED_ATTEMPTS,
  isPtyAuthRejection,
  shouldBlockPtyInput,
  shouldReconnectPtyOnPageResume,
  shouldRetryPtyClose,
} from "./pty-reconnect";

describe("shouldReconnectPtyOnPageResume", () => {
  it("reconnects a missing socket when the active page becomes visible", () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: true,
        socketReadyState: null,
        ptyState: "reconnecting",
      }),
    ).toBe(true);
  });

  it("reconnects closed or closing sockets on visible resume", () => {
    for (const socketReadyState of [2, 3]) {
      expect(
        shouldReconnectPtyOnPageResume({
          isActive: true,
          visibilityState: "visible",
          online: true,
          socketReadyState,
          ptyState: "reconnecting",
        }),
      ).toBe(true);
    }
  });

  it("does not reconnect an open socket on visible resume", () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: true,
        socketReadyState: 1,
        ptyState: "open",
      }),
    ).toBe(false);
  });

  it("reconnects a still-connecting socket when the page is already in reconnecting state", () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: true,
        socketReadyState: 0,
        ptyState: "reconnecting",
      }),
    ).toBe(true);
  });

  it("does not reconnect while the page is hidden", () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "hidden",
        online: true,
        socketReadyState: 3,
        ptyState: "reconnecting",
      }),
    ).toBe(false);
  });

  it("defers reconnect while offline", () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: false,
        socketReadyState: 3,
        ptyState: "reconnecting",
      }),
    ).toBe(false);
  });

  it("does not fire a redundant reconnect while a connect is in flight (wsRef not yet assigned)", () => {
    // The async socket-open IIFE has begun but not yet assigned wsRef, so
    // socketReadyState reads null. Without the connectInFlight guard this
    // would return true and double-connect.
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: true,
        socketReadyState: null,
        ptyState: "connecting",
        connectInFlight: true,
      }),
    ).toBe(false);
  });

  it("still reconnects an in-flight connect when the page already believes it is closed", () => {
    // A stuck attempt the user is actively trying to recover (manual reconnect
    // or a closed state) must not be suppressed by the in-flight guard.
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: "visible",
        online: true,
        socketReadyState: null,
        ptyState: "closed",
        connectInFlight: true,
      }),
    ).toBe(true);
  });
});

describe("isPtyAuthRejection", () => {
  it("recognises only the gateway's credential-rejected close code", () => {
    expect(isPtyAuthRejection(PTY_CLOSE_AUTH_REJECTED)).toBe(true);
    expect(isPtyAuthRejection(PTY_CLOSE_ABNORMAL)).toBe(false);
    expect(isPtyAuthRejection(4403)).toBe(false);
    expect(isPtyAuthRejection(1000)).toBe(false);
    expect(isPtyAuthRejection(null)).toBe(false);
    expect(isPtyAuthRejection(undefined)).toBe(false);
  });
});

describe("shouldRetryPtyClose", () => {
  it("gives a rejected credential exactly one more chance, then stops", () => {
    // A gated-mode ticket is single-use with a 30s TTL, so the first 4401
    // can mean "late": the next attempt mints a fresh one and may succeed.
    expect(
      shouldRetryPtyClose({
        code: PTY_CLOSE_AUTH_REJECTED,
        unopenedAttempts: 1,
        authRejections: 1,
      }),
    ).toBe(true);

    // Rejected twice in a row it is dead, not late. This is the founder's
    // case: the session token is minted per gateway process, so a tab opened
    // before a restart can never be accepted again, and retrying forever is
    // what left the dashboard saying "Reconnecting..." indefinitely.
    expect(
      shouldRetryPtyClose({
        code: PTY_CLOSE_AUTH_REJECTED,
        unopenedAttempts: 2,
        authRejections: 2,
      }),
    ).toBe(false);
  });

  it("does not let the unopened bound rescue a dead credential", () => {
    // Auth is decided by the auth counter alone: a tab whose first attempts
    // are rejected must stop even though it is nowhere near the
    // unopened-attempt bound.
    expect(
      shouldRetryPtyClose({
        code: PTY_CLOSE_AUTH_REJECTED,
        unopenedAttempts: 2,
        authRejections: PTY_MAX_AUTH_RETRIES + 1,
      }),
    ).toBe(false);
  });

  it("keeps retrying a genuine network 1006", () => {
    // The negative control: a real transport drop (sleep/wake, radio
    // handoff, gateway bounce) must not be mistaken for an auth failure, or
    // the NS-591 half-open-socket recovery regresses into a dead tab.
    expect(
      shouldRetryPtyClose({
        code: PTY_CLOSE_ABNORMAL,
        unopenedAttempts: 1,
        authRejections: 0,
      }),
    ).toBe(true);
  });

  it("keeps retrying every attempt below the unopened bound", () => {
    for (
      let unopenedAttempts = 1;
      unopenedAttempts < PTY_MAX_UNOPENED_ATTEMPTS;
      unopenedAttempts += 1
    ) {
      expect(
        shouldRetryPtyClose({
          code: PTY_CLOSE_ABNORMAL,
          unopenedAttempts,
          authRejections: 0,
        }),
      ).toBe(true);
    }
  });

  it("gives up once nothing has opened for the whole bound", () => {
    // Fallback for a gateway too old to send 4401: it refuses the upgrade
    // before accepting it, so the browser only ever reports a bare 1006 and
    // the tab would otherwise reconnect forever.
    for (const unopenedAttempts of [
      PTY_MAX_UNOPENED_ATTEMPTS,
      PTY_MAX_UNOPENED_ATTEMPTS + 5,
    ]) {
      expect(
        shouldRetryPtyClose({
          code: PTY_CLOSE_ABNORMAL,
          unopenedAttempts,
          authRejections: 0,
        }),
      ).toBe(false);
    }
  });

  it("retries a pre-socket failure that carries no close code", () => {
    // A ticket request that rejects or hangs leaves no socket, so there is
    // no code to reason about. It is still a bounded retry, not a give-up.
    expect(
      shouldRetryPtyClose({
        code: null,
        unopenedAttempts: 1,
        authRejections: 0,
      }),
    ).toBe(true);
    expect(
      shouldRetryPtyClose({
        code: null,
        unopenedAttempts: PTY_MAX_UNOPENED_ATTEMPTS,
        authRejections: 0,
      }),
    ).toBe(false);
  });

  it("retries a drop that follows a working session", () => {
    // `unopenedAttempts` is zeroed by `onopen`, so an hour-long session that
    // then drops arrives here as attempt 1 and must retry.
    expect(
      shouldRetryPtyClose({
        code: PTY_CLOSE_ABNORMAL,
        unopenedAttempts: 1,
        authRejections: 0,
      }),
    ).toBe(true);
  });
});

describe("shouldBlockPtyInput", () => {
  it("allows input only while the PTY socket is open", () => {
    expect(shouldBlockPtyInput("open")).toBe(false);
    expect(shouldBlockPtyInput("connecting")).toBe(true);
    expect(shouldBlockPtyInput("reconnecting")).toBe(true);
    expect(shouldBlockPtyInput("closed")).toBe(true);
    expect(shouldBlockPtyInput("ended")).toBe(true);
  });
});
