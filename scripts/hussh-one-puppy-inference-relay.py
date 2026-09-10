#!/usr/bin/env python3
"""Run Puppy One's bounded, inference-only outbound relay client."""

from __future__ import annotations

import asyncio

from gateway.puppy_inference_relay import run_puppy_inference_relay


if __name__ == "__main__":
    asyncio.run(run_puppy_inference_relay())
