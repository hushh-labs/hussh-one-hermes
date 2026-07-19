#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
# Backward-compatible wrapper for the Hussh One supervisor.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$SCRIPT_DIR/hussh-one-supervisor.sh" restart "$@"
