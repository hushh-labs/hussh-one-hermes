#!/usr/bin/env python3
"""Run Puppy One's bounded, inference-only outbound relay client."""

from __future__ import annotations

import asyncio
import argparse

from gateway.puppy_inference_relay import run_puppy_inference_relay


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run Puppy One's inference-only device relay"
    )
    parser.add_argument(
        "--direct", action="store_true", help="connect to this owner's BYOC pod"
    )
    args = parser.parse_args()
    if args.direct:
        from hermes_constants import get_hermes_home
        from hermes_cli.hussh_one_pkm.client import HusshIdentityClient
        from gateway.puppy_direct_pod import PuppyDirectPodRelay

        asyncio.run(
            PuppyDirectPodRelay(
                HusshIdentityClient(profile_home=get_hermes_home())
            ).serve()
        )
    else:
        asyncio.run(run_puppy_inference_relay())
