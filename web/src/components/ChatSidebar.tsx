/**
 * ChatSidebar — structured-events panel that sits next to the xterm.js
 * terminal in the dashboard Chat tab.
 *
 * Two WebSockets, one per concern:
 *
 *   1. **JSON-RPC sidecar** (`GatewayClient` → /api/ws) — drives the
 *      sidebar's own slot of the dashboard's in-process gateway.  Owns
 *      the model badge / picker / connection state / error banner.
 *      Independent of the PTY pane's session by design — those are the
 *      pieces the sidebar needs to be able to drive directly (model
 *      switch via slash.exec, etc.).
 *
 *   2. **Event subscriber** (/api/events?channel=…) — passive, receives
 *      every dispatcher emit from the PTY-side `tui_gateway.entry` that
 *      the dashboard fanned out.  This is how `tool.start/progress/
 *      complete` from the agent loop reach the sidebar even though the
 *      PTY child runs three processes deep from us.  The `channel` id
 *      ties this listener to the same chat tab's PTY child — see
 *      `ChatPage.tsx` for where the id is generated.
 *
 * Best-effort throughout: WS failures show in the badge / banner, the
 * terminal pane keeps working unimpaired.
 */

import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card } from "@nous-research/ui/ui/components/card";

import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";
import { HERMES_BASE_PATH, buildWsAuthParam } from "@/lib/api";

import { cn } from "@/lib/utils";
import { AlertCircle, ChevronDown, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

interface SessionInfo {
  cwd?: string;
  model?: string;
  provider?: string;
  credential_warning?: string;
}

interface RpcEnvelope {
  method?: string;
  params?: { type?: string; payload?: unknown };
}

const STATE_LABEL: Record<ConnectionState, string> = {
  idle: "idle",
  connecting: "connecting",
  open: "live",
  closed: "closed",
  error: "error",
};

const STATE_TONE: Record<
  ConnectionState,
  "secondary" | "warning" | "success" | "destructive"
> = {
  idle: "secondary",
  connecting: "warning",
  open: "success",
  closed: "secondary",
  error: "destructive",
};

interface ChatSidebarProps {
  channel: string;
  className?: string;
}

/** Build the ``session.create`` params for the sidecar session.
 *
 * Extracted from the effect below so the invariant — close_on_disconnect
 * is set, source is "tool", and the profile is forwarded when present —
 * can be tested without reading component source text. See
 * ``chat-sidebar-session-params.test.ts``.
 */
export function sidecarSessionCreateParams(profile?: string): Record<string, unknown> {
  return {
    close_on_disconnect: true,
    source: "tool",
    ...(profile ? { profile } : {}),
  };
}

export function ChatSidebar({
  channel,
  profile,
  className,
  onDashboardNewSessionRequest,
  onSessionTitleChange,
}: ChatSidebarProps) {
  // `version` bumps on reconnect; gw is derived so we never call setState
  // for it inside an effect (React 19's set-state-in-effect rule). The
  // counter is the dependency on purpose — it's not read in the memo body,
  // it's the signal that says "rebuild the client".
  const [version, setVersion] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const gw = useMemo(() => new GatewayClient(), [version]);

  const [state, setState] = useState<ConnectionState>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [info, setInfo] = useState<SessionInfo>({});
  const [modelOpen, setModelOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const offState = gw.onState(setState);

    const offSessionInfo = gw.on<SessionInfo>("session.info", (ev) => {
      if (ev.session_id) {
        setSessionId(ev.session_id);
      }

      if (ev.payload) {
        setInfo((prev) => ({ ...prev, ...ev.payload }));
      }
    });

    const offError = gw.on<{ message?: string }>("error", (ev) => {
      const message = ev.payload?.message;

      if (message) {
        setError(message);
      }
    });

    // Adopt whichever session the gateway hands us. session.create on the
    // sidecar is independent of the PTY pane's session by design — we
    // only need a sid to drive the model picker's slash.exec calls.
    gw.connect()
      .then(() => {
        if (cancelled) {
          return;
        }
        // close_on_disconnect: the gateway reaps this sidecar session (and its
        // slash_worker subprocess) when the WS drops, instead of leaking it.
        return gw.request<{ session_id: string }>("session.create", sidecarSessionCreateParams(profile));
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setError(e.message);
        }
      });

    return () => {
      cancelled = true;
      offState();
      offSessionInfo();
      offError();
      gw.close();
    };
  }, [gw]);

  // Event subscriber WebSocket — receives the rebroadcast of every
  // dispatcher emit from the PTY child's gateway.  See /api/pub +
  // /api/events in hermes_cli/web_server.py for the broadcast hop.
  //
  // Failures (auth/loopback rejection, server too old to expose the
  // endpoint, transient drops) surface in the same banner as the
  // JSON-RPC sidecar so the sidebar matches its documented best-effort
  // UX and the user always has a reconnect affordance.
  useEffect(() => {
    if (!channel) {
      return;
    }

    let unmounting = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    let attempt = 0;
    let connecting = false;

    const FATAL_CODES = new Set([4401, 4403]);
    const HEARTBEAT_MS = 15000;
    const BASE_BACKOFF_MS = 500;
    const MAX_BACKOFF_MS = 15000;
    const DISCONNECTED = "events feed disconnected — tool calls may not appear";

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const stopHeartbeat = () => {
      if (heartbeatTimer !== null) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    const startHeartbeat = (socket: WebSocket) => {
      stopHeartbeat();
      heartbeatTimer = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          try {
            socket.send("\x1b[PING]");
          } catch {
            // ignore
          }
        }
      }, HEARTBEAT_MS);
    };

    const scheduleReconnect = () => {
      if (unmounting) return;
      clearReconnectTimer();
      const ceiling = Math.min(
        MAX_BACKOFF_MS,
        BASE_BACKOFF_MS * 2 ** Math.min(attempt, 10),
      );
      const delay = Math.round(Math.random() * ceiling);
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (unmounting || connecting) return;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
      }
      connecting = true;

      let authParam: [string, string];
      try {
        authParam = await buildWsAuthParam();
      } catch (err) {
        connecting = false;
        console.warn(
          `[events] Auth-param fetch failed: ${(err as Error).message}`,
        );
        scheduleReconnect();
        return;
      }

      if (unmounting) {
        connecting = false;
        return;
      }

      const [authName, authValue] = authParam;
      if (!authValue) {
        connecting = false;
        return;
      }

      try {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const qs = new URLSearchParams({ [authName]: authValue, channel });
        ws = new WebSocket(
          `${proto}//${window.location.host}${HERMES_BASE_PATH}/api/events?${qs.toString()}`,
        );
      } catch (err) {
        connecting = false;
        console.warn(`[events] WebSocket construct failed: ${(err as Error).message}`);
        scheduleReconnect();
        return;
      }

      ws.addEventListener("open", () => {
        connecting = false;
        attempt = 0;
        setError(null);
        if (ws) {
          startHeartbeat(ws);
        }
      });

      ws.addEventListener("error", () => {
        // close handles reconnect
      });

      ws.addEventListener("close", (ev) => {
        connecting = false;
        stopHeartbeat();
        ws = null;
        if (unmounting) return;

        const why = ev.reason ? ` reason=${ev.reason}` : "";
        console.warn(`[events] WebSocket closed code=${ev.code}${why}`);

        if (FATAL_CODES.has(ev.code)) {
          if (ev.code === 4401 || ev.code === 4403) {
            setError(`events feed rejected (${ev.code}) — reload the page`);
          }
          return;
        }

        setError(DISCONNECTED);
        scheduleReconnect();
      });

      ws.addEventListener("message", (ev) => {
        let frame: RpcEnvelope;

        try {
          frame = JSON.parse(ev.data);
        } catch {
          return;
        }

        if (frame.method !== "event" || !frame.params) {
          return;
        }

        const { type, payload } = frame.params;

        if (type === "session.info") {
          if (payload) {
            setInfo((prev) => ({ ...prev, ...(payload as SessionInfo) }));
          }
        }
      });
    };

    void connect();

    return () => {
      unmounting = true;
      clearReconnectTimer();
      stopHeartbeat();
      if (ws) {
        ws.close();
      }
    };
  }, [channel, version]);

  const reconnect = useCallback(() => {
    setError(null);
    setVersion((v) => v + 1);
  }, []);

  // Picker hands us a fully-formed slash command (e.g. "/model anthropic/...").
  // Fire-and-forget through `slash.exec`; the TUI pane will render the result
  // via PTY, so the sidebar doesn't need to surface output of its own.
  const onModelSubmit = useCallback(
    (slashCommand: string) => {
      if (!sessionId) {
        return;
      }

      void gw.request("slash.exec", {
        session_id: sessionId,
        command: slashCommand,
      });
      setModelOpen(false);
    },
    [gw, sessionId],
  );

  const canPickModel = state === "open" && !!sessionId;
  const modelLabel = (info.model ?? "—").split("/").slice(-1)[0] ?? "—";
  const banner = error ?? info.credential_warning ?? null;

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 shrink-0 flex-col gap-3 overflow-y-auto overflow-x-hidden pr-1 lg:w-80",
        className,
      )}
    >
      <Card className="flex items-center justify-between gap-2 px-3 py-2">
        <div className="min-w-0">
          <div className="text-display text-xs tracking-wider text-text-tertiary">
            model
          </div>

          <Button
            ghost
            size="sm"
            disabled={!canPickModel}
            onClick={() => setModelOpen(true)}
            suffix={
              canPickModel ? (
                <ChevronDown className="text-text-secondary" />
              ) : undefined
            }
            className="self-start min-w-0 px-0 py-0 normal-case tracking-normal text-sm font-medium hover:underline disabled:no-underline"
            title={info.model ?? "switch model"}
          >
            <span className="truncate">{modelLabel}</span>
          </Button>
        </div>

        <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
      </Card>

      {banner && (
        <Card className="flex items-start gap-2 border-destructive/40 bg-destructive/5 px-3 py-2 text-xs">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />

          <div className="min-w-0 flex-1">
            <div className="wrap-break-word text-destructive">{banner}</div>

            {error && (
              <Button
                size="sm"
                outlined
                className="mt-1"
                onClick={reconnect}
                prefix={<RefreshCw />}
              >
                reconnect tools feed
              </Button>
            )}
          </div>
        </Card>
      )}

      {modelOpen && canPickModel && sessionId && (
        <ModelPickerDialog
          gw={gw}
          sessionId={sessionId}
          onClose={() => setModelOpen(false)}
          onSubmit={onModelSubmit}
        />
      )}
    </aside>
  );
}
