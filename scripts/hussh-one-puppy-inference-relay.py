#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Run Puppy One's bounded, inference-only outbound relay client."""

from __future__ import annotations

import asyncio
import argparse
import logging
import sys

from gateway.puppy_inference_relay import run_puppy_inference_relay


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run Puppy One's inference-only device relay"
    )
    parser.add_argument(
        "--direct", action="store_true", help="connect to this owner's BYOC pod"
    )
    parser.add_argument(
        "--wait-for-activation", action="store_true",
        help="with --direct, wait on the device control lane before requesting pod access",
    )
    args = parser.parse_args()
    if args.wait_for_activation and not args.direct:
        parser.error("--wait-for-activation requires --direct")
    if args.direct:
        # Only lifecycle codes and aggregate timings from these two modules.
        # HTTP clients and identity helpers retain their normal warning level.
        logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
        logging.getLogger("gateway.puppy_direct_pod").setLevel(logging.INFO)
        logging.getLogger("gateway.puppy_inference_relay").setLevel(logging.INFO)
        logging.getLogger("gateway.puppy_direct_service").setLevel(logging.INFO)
        from gateway.puppy_direct_service import run_profile_direct_relay
        from gateway.puppy_direct_pod import DirectPodRefused
        from hermes_cli.hussh_one_pkm.client import HusshIdentityError

        try:
            asyncio.run(run_profile_direct_relay(
                wait_for_activation=args.wait_for_activation, supervised=False,
            ))
        except KeyboardInterrupt:
            sys.exit(0)
        except ValueError:
            print("Select a local model with a loopback endpoint in this Hermes profile before starting Puppy.", file=sys.stderr)
            sys.exit(1)
        except DirectPodRefused as exc:
            print(f"Puppy could not connect: {exc}", file=sys.stderr)
            sys.exit(1)
        except HusshIdentityError:
            print("Log in and finish account setup in the Hussh app, then reconnect this Hermes profile.", file=sys.stderr)
            sys.exit(1)
    else:
        asyncio.run(run_puppy_inference_relay())
