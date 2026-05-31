import { AlertTriangle, Terminal } from "lucide-react";
import { PluginSlot } from "@/plugins";
import { cn } from "@/lib/utils";

export default function ChatDisabledPage() {
  return (
    <div
      className={cn(
        "flex min-h-0 w-full min-w-0 flex-1 flex-col",
        "px-4 py-8 sm:px-6 lg:px-8",
      )}
    >
      <PluginSlot name="chat:top" />
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center gap-6">
        <div className="flex items-start gap-4">
          <div className="mt-1 flex h-10 w-10 shrink-0 items-center justify-center rounded border border-current/20 bg-midground/5 text-midground">
            <Terminal className="h-5 w-5" />
          </div>
          <div className="min-w-0 space-y-2">
            <h1 className="text-xl font-semibold text-midground sm:text-2xl">
              Chat requires the embedded TUI
            </h1>
            <p className="max-w-2xl text-sm leading-6 text-text-secondary">
              The dashboard is running, but its Chat tab was not enabled for
              this process. Start the dashboard with{" "}
              <code className="rounded border border-current/15 px-1.5 py-0.5 text-xs text-midground">
                hermes dashboard --tui
              </code>{" "}
              or set{" "}
              <code className="rounded border border-current/15 px-1.5 py-0.5 text-xs text-midground">
                HERMES_DASHBOARD_TUI=1
              </code>{" "}
              before launch.
            </p>
          </div>
        </div>

        <div className="grid gap-3 rounded border border-current/15 bg-midground/[0.03] p-4 text-sm text-text-secondary">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
            <p className="leading-6">
              If the server reports PTY support is unavailable, install the
              PTY extra with{" "}
              <code className="rounded border border-current/15 px-1.5 py-0.5 text-xs text-midground">
                pip install &apos;hermes-agent[web,pty]&apos;
              </code>
              . On native Windows, run the dashboard from WSL2 for embedded
              terminal support.
            </p>
          </div>
          <p className="leading-6">
            The chat backend remains disabled until one of those options is
            used. The protected{" "}
            <code className="rounded border border-current/15 px-1.5 py-0.5 text-xs text-midground">
              /api/pty
            </code>{" "}
            endpoint is still gated and will not spawn a TUI from this
            dashboard process.
          </p>
        </div>
      </div>
      <PluginSlot name="chat:bottom" />
    </div>
  );
}
