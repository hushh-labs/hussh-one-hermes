# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Request-owned local transports: cancellation never frees another call's slot."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


class LocalAuxiliaryRequest:
    def __init__(self, client: Any, acquire: Callable[[], Any], verify: Any = True, purpose: str = "auxiliary"):
        self.shared = client
        self.acquire = acquire
        self.verify = verify
        self.purpose = purpose if purpose in {"compression", "vision", "browser_vision", "memory", "auxiliary"} else "auxiliary"
        self.priority = "interactive" if self.purpose in {"vision", "browser_vision"} else "background"
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._client = None
        self._watcher_started = False
        self.request_id = uuid.uuid4().hex

    def _check(self) -> None:
        from agent.auxiliary_client import AuxiliaryExplicitCancellation, _aux_interrupt_cancel_requested
        if self.cancelled.is_set() or _aux_interrupt_cancel_requested():
            raise AuxiliaryExplicitCancellation()

    def run(self, request: dict, create: Callable[[Any, dict], Any]) -> Any:
        from agent.process_bootstrap import build_keepalive_http_client
        lease = None
        owned = None
        try:
            self._check()
            queued_at = time.monotonic()
            lease = self.acquire()
            logger.info("local_auxiliary request=%s purpose=%s priority=%s phase=admitted queued_seconds=%.3f",
                        self.request_id, self.purpose, self.priority, time.monotonic() - queued_at)
            self._check()
            transport = build_keepalive_http_client(str(self.shared.base_url), verify=self.verify)
            if transport is None:
                raise RuntimeError("Cannot create an independent local auxiliary transport")
            try:
                owned = self.shared.copy(http_client=transport, max_retries=0)
            except BaseException:
                transport.close()
                raise
            with self._lock:
                self._client = owned
            self._check()
            logger.info("local_auxiliary request=%s phase=running", self.request_id)
            return create(owned, request)
        finally:
            # Only this request worker closes descriptors and releases capacity.
            # The cancellation owner cannot do either while this call is live.
            try:
                if owned is not None:
                    owned.close()
            finally:
                try:
                    if lease is not None:
                        lease.release()
                        logger.info("local_auxiliary request=%s permit=released", self.request_id)
                finally:
                    self.done.set()
                    logger.info("local_auxiliary request=%s phase=terminated cancelled=%s", self.request_id, self.cancelled.is_set())

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            if self._watcher_started or self.done.is_set():
                return
            self._watcher_started = True
        logger.info("local_auxiliary request=%s phase=cancelling capacity=retained", self.request_id)

        def abort_until_done() -> None:
            from agent.agent_runtime_helpers import force_close_tcp_sockets
            # A connection may register after cancellation. Repeat shutdown until
            # the finite provider timeout or worker cleanup completes. Never close
            # the shared client and never release admission in this thread.
            reported = False
            while not self.done.is_set():
                with self._lock:
                    client = self._client
                if client is not None:
                    try:
                        count = force_close_tcp_sockets(client)
                        if not reported:
                            logger.info("local_auxiliary request=%s sockets_shutdown=%s capacity=retained_until_worker_exit", self.request_id, count)
                            reported = True
                    except Exception:
                        logger.warning("local_auxiliary request=%s abort_failed capacity=retained", self.request_id)
                self.done.wait(0.05)

        threading.Thread(target=abort_until_done, name="hermes-local-aux-abort", daemon=True).start()
