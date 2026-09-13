# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Device and workload presets (pure, no I/O).

Capacities are conservative *usable* figures, not spec-sheet maxima — the OS and
the person's foreground work keep their share, and ``placement`` applies its own
safety fraction on top of these.
"""

from __future__ import annotations

from .types import DeviceProfile, WorkloadEstimate, WorkloadPreset

DEVICE_PROFILES: list[DeviceProfile] = [
    DeviceProfile("iphone-17-pro", "iPhone 17 Pro", 6, 6, 12, 128, 1_000),
    DeviceProfile("ipad-pro-m4", "iPad Pro (M4)", 10, 10, 16, 256, 1_200),
    DeviceProfile("macbook-pro-m4-max", "MacBook Pro (M4 Max)", 16, 40, 128, 1_024, 2_000),
    DeviceProfile("mac-studio-m3-ultra", "Mac Studio (M3 Ultra)", 32, 80, 192, 2_048, 10_000),
    # Discrete-GPU machines: VRAM is the binding accelerator budget, not system RAM.
    DeviceProfile("windows-laptop", "Windows laptop (RTX 4070)", 14, 0, 8, 512, 1_000),
    DeviceProfile("windows-workstation", "Windows workstation (RTX 4090)", 24, 0, 24, 2_048, 2_000),
]


def find_device_profile(device_id: str) -> DeviceProfile | None:
    return next((d for d in DEVICE_PROFILES if d.id == device_id), None)


WORKLOAD_PRESETS: list[WorkloadPreset] = [
    WorkloadPreset(
        "photos-model", "📸", "Train on my photos",
        "A private model of your 5,000 photos", "gpu", 25, 1,
        WorkloadEstimate(vram_gb=12, unified_memory_gb=16, vcpus=8, disk_gb=20,
                         estimated_minutes=25),
    ),
    WorkloadPreset(
        "clip-edit", "🎬", "Enhance a 4K clip",
        "On-device when your machine can take it", "gpu", 6, 1,
        WorkloadEstimate(vram_gb=4, unified_memory_gb=6, vcpus=4, disk_gb=8,
                         estimated_minutes=6),
    ),
    WorkloadPreset(
        "finetune-70b", "🧠", "Fine-tune the full 70B",
        "The whole model — not a shrunk proxy", "gpu", 90, 4,
        WorkloadEstimate(vram_gb=640, unified_memory_gb=256, vcpus=48, disk_gb=400,
                         estimated_minutes=90),
    ),
    WorkloadPreset(
        "render-film", "🎥", "Render a film sequence",
        "4K frames, overnight → over coffee", "gpu", 40, 2,
        WorkloadEstimate(vram_gb=48, unified_memory_gb=64, vcpus=16, disk_gb=250,
                         estimated_minutes=40),
    ),
    WorkloadPreset(
        "backtest-markets", "📈", "Backtest 10 years of markets",
        "Every tick, the full history", "gpu", 20, 1,
        WorkloadEstimate(vram_gb=24, unified_memory_gb=96, vcpus=32, disk_gb=5_000,
                         estimated_minutes=20),
    ),
    WorkloadPreset(
        "fold-protein", "🧬", "Fold a protein",
        "TPU-class science from your pocket", "tpu", 30, 2,
        WorkloadEstimate(vram_gb=100, unified_memory_gb=128, vcpus=24, disk_gb=100,
                         estimated_minutes=30),
    ),
    WorkloadPreset(
        "frontier-run", "🚀", "A frontier training run",
        "Blackwell-class — the biggest GCP offers", "gpu", 240, 8,
        WorkloadEstimate(vram_gb=1_000, unified_memory_gb=512, vcpus=96, disk_gb=2_000,
                         estimated_minutes=240),
    ),
]


def find_workload_preset(preset_id: str) -> WorkloadPreset | None:
    return next((p for p in WORKLOAD_PRESETS if p.id == preset_id), None)
