// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { buildTerminalTheme } from "./terminal-theme";

describe("buildTerminalTheme", () => {
  it("propagates the typed theme foreground to xterm body and cursor", () => {
    expect(buildTerminalTheme("#071312", "#DDE7EA")).toMatchObject({
      background: "#071312",
      cursor: "#DDE7EA",
      foreground: "#DDE7EA",
      selectionBackground: "#DDE7EA44",
    });
  });
});
