# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Profile-owned lifecycle for the existing approved-device inference relay."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_active_profiles: set[Path] = set()
_LOCK_SCOPE = "puppy-direct-relay"


async def run_profile_direct_relay(
    *, wait_for_activation: bool = True, supervised: bool = True,
) -> None:
    """Share one relay between gateway supervision and the explicit launcher.

    Gateway startup is opt-in. Activation still obtains the current owner grant
    and fresh pod admission; local configuration never grants inference access.
    """
    from hermes_cli.config import load_config_readonly

    try:
        config = load_config_readonly()
    except Exception:
        if not supervised:
            raise
        logger.warning("puppy_direct.profile_configuration_unavailable")
        return
    hussh = config.get("hussh_one")
    puppy = hussh.get("puppy") if isinstance(hussh, dict) else None
    if supervised and (not isinstance(puppy, dict) or puppy.get("auto_start") is not True):
        return

    from gateway.puppy_direct_pod import DirectPodRefused, PuppyDirectPodRelay
    from gateway.puppy_inference_relay import profile_model_options
    from gateway.status import acquire_scoped_lock, release_scoped_lock
    from hermes_cli.hussh_one_pkm.client import HusshIdentityClient, HusshIdentityError
    from hermes_cli.hussh_one_pkm.presence import (
        PresencePublisher, agent_version, build_snapshot,
    )
    from hermes_constants import get_hermes_home
    import httpx

    home = get_hermes_home().resolve()
    if home in _active_profiles:
        if supervised:
            return
        raise DirectPodRefused("Puppy relay is already running for this profile")
    acquired, _ = acquire_scoped_lock(_LOCK_SCOPE, str(home), require_gateway=False)
    if not acquired:
        if supervised:
            logger.info("puppy_direct.profile_already_running")
            return
        raise DirectPodRefused("Puppy relay is already running for this profile")

    _active_profiles.add(home)
    transport = None
    try:
        options = profile_model_options(config)
        transport = httpx.Client(timeout=30.0)
        identity = HusshIdentityClient(profile_home=home, http=transport)
        state = identity.read_state()
        if state is None:
            raise HusshIdentityError("Connect this Hermes profile to Hussh first")
        logger.info("puppy_direct.profile_selected environment=%s", state.environment)

        def current_model() -> str:
            if get_hermes_home().resolve() != home:
                return options["model"]
            try:
                return profile_model_options(load_config_readonly())["model"]
            except ValueError:
                return options["model"]

        presence = PresencePublisher(
            publish=identity.post_heartbeat,
            snapshot=lambda: build_snapshot(
                current_model=current_model(), agent_version=agent_version(), home=home,
            ),
        )
        await PuppyDirectPodRelay(
            identity, wait_for_activation=wait_for_activation, presence=presence, **options,
        ).serve()
    except (DirectPodRefused, HusshIdentityError, ValueError):
        if not supervised:
            raise
        # A clean return deliberately disables supervisor retries after a
        # revocation, identity refusal or invalid local-model configuration.
        logger.warning("puppy_direct.profile_requires_owner_review")
    except Exception:
        # The supervisor may retry a transient failure, but must not log a
        # provider exception carrying credentials or a private endpoint.
        raise RuntimeError("Puppy relay service failed") from None
    finally:
        try:
            if transport is not None:
                transport.close()
        finally:
            _active_profiles.discard(home)
            release_scoped_lock(_LOCK_SCOPE, str(home))
