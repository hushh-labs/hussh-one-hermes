/** Build the xterm theme used by the dashboard's embedded Hussh TUI. */
export function buildTerminalTheme(background?: string, foreground?: string) {
  const resolvedBackground = background || "#000000";
  const resolvedForeground = foreground || "#f0e6d2";
  return {
    background: resolvedBackground,
    cursor: resolvedForeground,
    cursorAccent: "#0d2626",
    foreground: resolvedForeground,
    selectionBackground: `${resolvedForeground}44`,
  };
}
