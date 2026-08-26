// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0

/**
 * Dashboard PTY attachment identity.
 *
 * A keep-alive PTY belongs to one browser tab, not to every dashboard tab on
 * the origin. `localStorage` is origin-wide, so it made two chat tabs attach
 * to the same PTY: each socket superseded the other and both could forward a
 * keystroke during the hand-off. `sessionStorage` survives a reload in this
 * tab while keeping separately opened dashboard tabs independent.
 */
const PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";

function randomToken(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Return this tab's keep-alive PTY identity, rotating for a fresh chat. */
export function ptyAttachToken(rotate = false): string {
  let token = "";

  if (!rotate) {
    try {
      token = window.sessionStorage.getItem(PTY_ATTACH_TOKEN_KEY) ?? "";
    } catch {
      // Private mode / storage-blocked browser: use an ephemeral token.
    }
  }

  if (!token) {
    token = randomToken();
    try {
      window.sessionStorage.setItem(PTY_ATTACH_TOKEN_KEY, token);
    } catch {
      // The token still scopes this page load even when storage is blocked.
    }
  }

  return token;
}
