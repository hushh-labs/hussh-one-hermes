// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0

import { HusshOneSetup } from '@/components/hussh-one/setup'

import { SettingsContent } from './primitives'

/** Persistent trusted-device control plane for every configured Hermes profile. */
export function HusshOneSettings() {
  return (
    <SettingsContent>
      <HusshOneSetup />
    </SettingsContent>
  )
}
