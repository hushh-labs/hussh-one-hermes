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
        from hermes_constants import get_hermes_home
        from hermes_cli.hussh_one_pkm.client import HusshIdentityClient, HusshIdentityError
        from hermes_cli.hussh_one_pkm.presence import PresencePublisher, agent_version, build_snapshot
        from gateway.puppy_direct_pod import PuppyDirectPodRelay, DirectPodRefused
        from gateway.puppy_inference_relay import profile_model_options
        from hermes_cli.config import load_config_readonly

        try:
            model_options = profile_model_options(load_config_readonly())
            profile_home = get_hermes_home()
            identity = HusshIdentityClient(profile_home=profile_home)
            state = identity.read_state()
            if state is not None:
                print(
                    f"Puppy direct relay selected the {state.environment} Hussh profile.",
                    file=sys.stderr, flush=True,
                )
            def current_model() -> str:
                try:
                    return profile_model_options(load_config_readonly())["model"]
                except ValueError:
                    return model_options["model"]

            presence = PresencePublisher(
                publish=identity.post_heartbeat,
                snapshot=lambda: build_snapshot(
                    current_model=current_model(),
                    agent_version=agent_version(),
                    home=profile_home,
                ),
            )
            if args.wait_for_activation:
                print(
                    "Waiting for Puppy activation. Enable Puppy for this device in "
                    "the Hussh app, then start an inference request. Owner approval "
                    "is still required before connecting to your pod.",
                    file=sys.stderr, flush=True,
                )
            asyncio.run(
                PuppyDirectPodRelay(
                    identity,
                    wait_for_activation=args.wait_for_activation,
                    presence=presence,
                    **model_options,
                ).serve()
            )
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
