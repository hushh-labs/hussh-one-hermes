"""Surface-neutral runtime identity for Hussh One.

The model label shown to a user must describe the effective route without
leaking credentials or making automatic routing look like a manual model pin.
This module is deliberately independent of WhatsApp, Ink, and the dashboard so
every surface applies the same `[A]`/`[S]` contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal, Mapping, Optional


SelectionMode = Literal["auto", "select"]
AUTO_MODE: SelectionMode = "auto"
SELECT_MODE: SelectionMode = "select"


def display_model_name(model: Optional[str]) -> str:
    """Return a concise, stable display name for a model identifier."""
    raw = (model or "").strip() or "gemini-3.5-flash"
    short = raw.rsplit("/", 1)[-1].lower()

    if "gemini" in short:
        match = re.search(r"gemini-(\d+(?:\.\d+)?)", short)
        version = match.group(1) if match else "3.5"
        variant = "Flash" if "flash" in short else "Pro" if "pro" in short else ""
        return f"Gemini {version} {variant}".strip()

    if "claude" in short or any(word in short for word in ("opus", "sonnet", "haiku", "fable")):
        variant = (
            "Opus" if "opus" in short else "Sonnet" if "sonnet" in short
            else "Haiku" if "haiku" in short else "Fable" if "fable" in short else ""
        )
        match = re.search(r"(\d+)[-.](\d+)", short)
        if match:
            version = f"{match.group(1)}.{match.group(2)}"
        else:
            single = re.search(r"(?:fable|sonnet|opus|haiku|claude)-(\d+)\b", short)
            # `claude-opus-4` is the Vertex catalog alias for the current
            # Opus 4.8 route. Keep the user-facing label precise rather than
            # regressing to the ambiguous `Claude Opus 4`.
            if variant == "Opus" and single and single.group(1) == "4":
                version = "4.8"
            elif single:
                version = single.group(1)
            else:
                version = "4.8" if variant == "Opus" else "3.5"
        return f"Claude {variant} {version}".strip()

    if "gemma" in short:
        match = re.search(r"gemma-(\d+)", short)
        version = match.group(1) if match else "4"
        size_match = re.search(r"-(\d+b|e2b|a4b|a3b)(?:-|$)", short)
        size = size_match.group(1).upper() if size_match else ""
        if size == "E2B":
            size = "e2b"
        elif size == "A4B":
            size = "26B a4b"
        return f"Gemma {version} {size}".strip()

    if "qwen" in short:
        match = re.search(r"qwen(\d+(?:\.\d+)?)", short)
        version = match.group(1) if match else "3.6"
        size_match = re.search(r"-(\d+b)(?:-|$)", short)
        variant_match = re.search(r"-(a3b|a4b)(?:-|$)", short)
        size = size_match.group(1).upper() if size_match else ""
        variant = variant_match.group(1) if variant_match else ""
        return f"Qwen {version} {size} {variant}".replace("  ", " ").strip()

    return short


def normalize_selection_mode(value: Any) -> SelectionMode:
    """Normalize persisted selection provenance; unknown/legacy state is auto."""
    return SELECT_MODE if value in (True, SELECT_MODE, "S", "[S]") else AUTO_MODE


def mode_token(selection_mode: SelectionMode | str | bool) -> str:
    return "[S]" if normalize_selection_mode(selection_mode) == SELECT_MODE else "[A]"


def selection_mode_from_override(override: Mapping[str, Any] | None) -> SelectionMode:
    """Read explicit selection provenance from a non-secret session override."""
    if not isinstance(override, Mapping):
        return AUTO_MODE
    return normalize_selection_mode(override.get("selection_mode"))


def route_label(
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """Return a safe routing label; never include tokens, projects, or URLs."""
    provider_value = (provider or "").strip().lower()
    base_url_value = (base_url or "").strip().lower()
    if (
        "vertex" in provider_value
        or provider_value in {"google", "google-ai"} and "aiplatform.googleapis.com" in base_url_value
        or "aiplatform.googleapis.com" in base_url_value
    ):
        return "Vertex ADC"
    return ""


@dataclass(frozen=True)
class HusshRuntimeIdentity:
    display_model: str
    route_label: str
    selection_mode: SelectionMode
    mode_token: str

    @property
    def label(self) -> str:
        parts = [self.display_model]
        if self.route_label:
            parts.append(self.route_label)
        parts.append(self.mode_token)
        return " · ".join(parts)

    def to_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["label"] = self.label
        return data


def resolve_runtime_identity(
    model: Optional[str],
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    selection_mode: SelectionMode | str | bool = AUTO_MODE,
) -> HusshRuntimeIdentity:
    mode = normalize_selection_mode(selection_mode)
    return HusshRuntimeIdentity(
        display_model=display_model_name(model),
        route_label=route_label(provider=provider, base_url=base_url),
        selection_mode=mode,
        mode_token=mode_token(mode),
    )
