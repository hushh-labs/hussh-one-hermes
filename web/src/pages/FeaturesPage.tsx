// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
import { useLayoutEffect } from "react";
import {
  Sparkles,
  MessageSquare,
  Globe,
  ShieldCheck,
  Cpu,
  Wrench,
  Brain,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";

/**
 * 🤫 Hussh One — Features page.
 *
 * Sidebar item → loads this in the main body (same pattern as DocsPage), but
 * rendered NATIVELY (typed cards), not an iframe. The catalog mirrors
 * docs/hussh-one/features/README.md and the contracts (invariants A–K). When
 * you ship a feature there, add a row here so the dashboard surface stays in
 * lock-step with the docs.
 */

type Status = "shipped" | "planned";

interface Feature {
  name: string;
  desc: string;
  surface: string;
  status: Status;
}

interface FeatureGroup {
  id: string;
  title: string;
  icon: LucideIcon;
  blurb: string;
  features: Feature[];
}

const SURFACES: { icon: LucideIcon; title: string; desc: string }[] = [
  {
    icon: Cpu,
    title: "TUI / Dashboard",
    desc: "hermes --tui plus the embedded real TUI in this dashboard.",
  },
  {
    icon: MessageSquare,
    title: "WhatsApp",
    desc: "Branded, owner-gated personal agent with capsules.",
  },
  {
    icon: Globe,
    title: "Open WebUI",
    desc: "Browser chat over the OpenAI-compatible API server.",
  },
];

const GROUPS: FeatureGroup[] = [
  {
    id: "whatsapp",
    title: "WhatsApp Layer",
    icon: MessageSquare,
    blurb: "Branded, owner-only, injection-proof messaging with sandboxed group capsules.",
    features: [
      { name: "Stacked brand header", desc: "3-line header (brand · model [A/S] · divider) on every send.", surface: "WhatsApp", status: "shipped" },
      { name: "Owner-only triggering", desc: "Injection-proof gating; strict @One tagging in groups and DMs.", surface: "WhatsApp", status: "shipped" },
      { name: "Multi-device (LID) auth", desc: "Authorizes your linked devices via JID/LID.", surface: "WhatsApp", status: "shipped" },
      { name: "Social-group capsules", desc: "Sandboxed: isolated memory, read-only toolset, no lateral sends.", surface: "WhatsApp", status: "shipped" },
      { name: "Anti-DOS rate limit", desc: "Non-owner capsule triggering with configurable rate caps.", surface: "WhatsApp", status: "shipped" },
      { name: "Clean output", desc: "No reasoning/logs/jargon; bold-only; autopilot approvals.", surface: "WhatsApp", status: "shipped" },
      { name: "Local data & recovery", desc: "WhatsApp history/media retrieval, message edit/recovery.", surface: "WhatsApp", status: "shipped" },
    ],
  },
  {
    id: "surfaces",
    title: "CLI / Web / API",
    icon: Globe,
    blurb: "Theming, model switching, and the Open WebUI browser variant.",
    features: [
      { name: "CLI/TUI + Dashboard theming", desc: "The hussh-one skin across terminal and web.", surface: "CLI/Web", status: "shipped" },
      { name: "Natural-language model switching", desc: '"switch to opus 4.8" — deterministic, injection-safe.', surface: "WhatsApp/TUI", status: "shipped" },
      { name: "Open WebUI browser chat variant", desc: "Full web chat over the OpenAI-compatible API server; 1 agent call per message.", surface: "Web", status: "shipped" },
      { name: "TUI model popover sync", desc: "Picker opens reflecting the live session model + active provider/model.", surface: "TUI", status: "shipped" },
    ],
  },
  {
    id: "reliability",
    title: "Reliability",
    icon: ShieldCheck,
    blurb: "Keeps long, heavy sessions correct and crash-free across every surface.",
    features: [
      { name: "Session-model persistence & resume", desc: "Sessions keep their model across refresh / --resume / cold restart.", surface: "TUI/Web/API", status: "shipped" },
      { name: "Vertex-Claude pinning", desc: "Claude always routes through GCP Vertex (ADC), never Anthropic-direct.", surface: "All", status: "shipped" },
      { name: "Dashboard crash resilience (OOM-safe)", desc: "Compaction tuning + supervisor RSS soft-cap → clean restart, never SIGKILL.", surface: "Web/Ops", status: "shipped" },
      { name: "Open WebUI optimization", desc: "Title/tag generation off by default → 1 agent call per message.", surface: "Web", status: "shipped" },
    ],
  },
];

const CONTRACTS: { id: string; label: string }[] = [
  { id: "A", label: "Group routing safeguard" },
  { id: "B", label: "Zero-width unicode leakage" },
  { id: "C", label: "Upstream update guard" },
  { id: "D", label: "Dashboard real-TUI" },
  { id: "E", label: "NL model switching" },
  { id: "F", label: "Capsule sandbox" },
  { id: "G", label: "Branding & header" },
  { id: "H", label: "Session-model resume" },
  { id: "I", label: "Dashboard crash resilience" },
  { id: "J", label: "TUI model popover sync" },
  { id: "K", label: "Open WebUI surface" },
];

const CORE_HIGHLIGHTS: { icon: LucideIcon; title: string; desc: string }[] = [
  { icon: Brain, title: "Closed learning loop", desc: "Curated memory, autonomous skills, FTS5 cross-session recall." },
  { icon: Wrench, title: "60+ tools", desc: "File, terminal (6 backends), web/browser, media gen, orchestration." },
  { icon: Globe, title: "20+ platforms", desc: "One gateway: CLI, Telegram, Discord, Slack, WhatsApp, and more." },
  { icon: Cpu, title: "Multi-provider", desc: "Nous Portal, OpenRouter, Vertex, Anthropic, Gemini, local + plugins." },
];

function StatusBadge({ status }: { status: Status }) {
  return status === "shipped" ? (
    <Badge tone="success">Shipped</Badge>
  ) : (
    <Badge tone="outline">Planned</Badge>
  );
}

function Card({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "rounded-lg border border-current/15 bg-card/40 p-4",
        className,
      )}
    >
      {children}
    </div>
  );
}

export default function FeaturesPage() {
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  useLayoutEffect(() => {
    setEnd(
      <Badge tone="outline" className="gap-1.5">
        <Sparkles className="size-3.5" />
        {GROUPS.reduce((n, g) => n + g.features.length, 0)} shipped features
      </Badge>,
    );
    return () => setEnd(null);
  }, [setEnd, t]);

  return (
    <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col gap-6 overflow-y-auto pt-1 pb-10 sm:pt-2">
      <PluginSlot name="features:top" />

      {/* Hero */}
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-2xl font-bold tracking-tight">
          <span aria-hidden>🤫</span>
          <span>Hussh One — Features</span>
        </div>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Hussh One is an overlay on Hermes Agent: a single, secure personal
          agent present across every surface. Every feature has a module, a
          config knob, a test, and a doc page.
        </p>
      </header>

      {/* Three surfaces */}
      <section className="flex flex-col gap-3">
        <h2 className="text-xs font-bold uppercase tracking-[0.2em] text-muted-foreground">
          Three first-class surfaces
        </h2>
        <div className="grid gap-3 sm:grid-cols-3">
          {SURFACES.map((s) => (
            <Card key={s.title} className="flex flex-col gap-2">
              <div className="flex items-center gap-2 font-semibold">
                <s.icon className="size-4 text-primary" />
                {s.title}
              </div>
              <p className="text-xs text-muted-foreground">{s.desc}</p>
            </Card>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          All three run the same agent, router, and models.
        </p>
      </section>

      {/* Feature groups */}
      {GROUPS.map((group) => (
        <section key={group.id} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <h2 className="flex items-center gap-2 text-sm font-bold">
              <group.icon className="size-4 text-primary" />
              {group.title}
            </h2>
            <p className="text-xs text-muted-foreground">{group.blurb}</p>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {group.features.map((f) => (
              <Card key={f.name} className="flex items-start justify-between gap-3">
                <div className="flex flex-col gap-1">
                  <div className="text-sm font-semibold">{f.name}</div>
                  <div className="text-xs text-muted-foreground">{f.desc}</div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground/70">
                    {f.surface}
                  </div>
                </div>
                <StatusBadge status={f.status} />
              </Card>
            ))}
          </div>
        </section>
      ))}

      {/* Contracts */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="flex items-center gap-2 text-sm font-bold">
            <ShieldCheck className="size-4 text-primary" />
            Deterministic contracts (A–K)
          </h2>
          <p className="text-xs text-muted-foreground">
            Machine-readable invariants every build must satisfy — each maps to a
            test or guard check.
          </p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {CONTRACTS.map((c) => (
            <Card key={c.id} className="flex items-center gap-3 py-2.5">
              <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-primary/15 text-xs font-bold text-primary">
                {c.id}
              </span>
              <span className="text-xs">{c.label}</span>
            </Card>
          ))}
        </div>
      </section>

      {/* Hermes core */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="flex items-center gap-2 text-sm font-bold">
            <Cpu className="size-4 text-primary" />
            Built on Hermes Agent
          </h2>
          <p className="text-xs text-muted-foreground">
            The upstream core Hussh One extends — by Nous Research.
          </p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {CORE_HIGHLIGHTS.map((h) => (
            <Card key={h.title} className="flex flex-col gap-2">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <h.icon className="size-4 text-primary" />
                {h.title}
              </div>
              <p className="text-xs text-muted-foreground">{h.desc}</p>
            </Card>
          ))}
        </div>
      </section>

      <PluginSlot name="features:bottom" />
    </div>
  );
}
