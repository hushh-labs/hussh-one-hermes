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

describe("chatPtyIdentity", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.unstubAllGlobals();
  });

  it("reconnects publisher and sidebar with the same tab-scoped pair", async () => {
    const { chatPtyIdentity } = await import("./pty-attach-token");
    const first = chatPtyIdentity(JSON.stringify(["session-a", "profile-a"]));
    vi.resetModules();
    const reloaded = await import("./pty-attach-token");
    expect(reloaded.chatPtyIdentity(JSON.stringify(["session-a", "profile-a"]))).toEqual(first);
    expect(first.channel).not.toContain(first.attach);
  });

  it("isolates profiles and resume targets and rotates both identities for a fresh chat", async () => {
    const { chatPtyIdentity } = await import("./pty-attach-token");
    const scope = JSON.stringify(["session-a", "profile-a"]);
    const first = chatPtyIdentity(scope);
    expect(chatPtyIdentity(JSON.stringify(["session-a", "profile-b"]))).not.toEqual(first);
    expect(chatPtyIdentity(JSON.stringify(["session-b", "profile-a"]))).not.toEqual(first);
    const fresh = chatPtyIdentity(scope, true);
    expect(fresh.attach).not.toBe(first.attach);
    expect(fresh.channel).not.toBe(first.channel);
    expect(chatPtyIdentity(scope)).toEqual(fresh);
  });
});
