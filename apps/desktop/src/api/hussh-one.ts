// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
//
// Hussh One identity/vault bridge (fork-owned). Kept out of upstream's
// api/*.ts split because it has no upstream equivalent — re-homed here so it
// survives the api/ refactor while following the same hermesApi/profileScoped
// call convention as the rest of api/.
import { getApiRequestProfile, hermesApi, profileScoped } from './client'

export interface HusshOneStatus {
  identity: {
    connected: boolean
    environment: string
    device_id: string | null
    account_email: string | null
    profile_id: string
  }
  vault: {
    connected: boolean
    enrolled: boolean
    unlocked: boolean
    profile_locked: boolean
    device_id: string | null
    profile_id: string
  }
  authorization: {
    status: 'idle' | 'waiting' | 'connected' | 'error'
    error?: string | null
  }
  onboarding: {
    remote_vault: 'not_connected' | 'available' | 'not_created' | 'unavailable'
  }
}

export function getHusshOneStatus(): Promise<HusshOneStatus> {
  return hermesApi<HusshOneStatus>({
    ...profileScoped(),
    path: '/api/hussh-one/status'
  })
}

export function connectHusshOne(deviceName = 'Hermes on Mac'): Promise<{
  status: 'waiting'
  authorization_url: string
  expires_in: number
}> {
  return hermesApi({
    ...profileScoped(),
    path: '/api/hussh-one/connect',
    method: 'POST',
    body: { device_name: deviceName }
  })
}

export function enrollHusshOneVault(profile?: null | string): Promise<{
  status?: 'canceled'
  enrolled: boolean
  unlocked: boolean
  contract_compatible: boolean
  native_connector_ready: boolean
}> {
  // Native vault enrollment is a dedicated preload bridge call (not a REST
  // request), so it bypasses hermesApi/connectionScoped and resolves the
  // profile itself.
  return window.hermesDesktop.enrollHusshOneVault(profile ?? getApiRequestProfile())
}

export function unlockHusshOneVault(): Promise<{ unlocked: boolean }> {
  return hermesApi({
    ...profileScoped(),
    path: '/api/hussh-one/vault/unlock',
    method: 'POST'
  })
}

export function lockHusshOneVault(): Promise<{ locked: boolean }> {
  return hermesApi({
    ...profileScoped(),
    path: '/api/hussh-one/vault/lock',
    method: 'POST'
  })
}

export function disconnectHusshOne(): Promise<{ connected: boolean; revoked: boolean }> {
  return hermesApi({
    ...profileScoped(),
    path: '/api/hussh-one/connection',
    method: 'DELETE'
  })
}
