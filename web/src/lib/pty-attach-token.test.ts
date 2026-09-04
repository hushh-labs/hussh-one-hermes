// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ptyAttachToken } from "./pty-attach-token";

describe("ptyAttachToken", () => {
  beforeEach(() => {
    sessionStorage.clear();
    let next = 0;
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(++next);
        return bytes;
      },
    });
  });

  it("persists the keep-alive identity only for this browser tab", () => {
    const first = ptyAttachToken();

    expect(ptyAttachToken()).toBe(first);
    expect(sessionStorage.getItem("hermes.pty.token.chat")).toBe(first);
  });

  it("rotates the identity for an explicit fresh chat", () => {
    const first = ptyAttachToken();
    const fresh = ptyAttachToken(true);

    expect(fresh).not.toBe(first);
    expect(ptyAttachToken()).toBe(fresh);
  });
});
