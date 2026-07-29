import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { HusshOneSetup, husshOneStatusLabel } from './setup'

const connectHusshOne = vi.fn()
const disconnectHusshOne = vi.fn()
const enrollHusshOneVault = vi.fn()
const getHusshOneStatus = vi.fn()
const lockHusshOneVault = vi.fn()
const setApiRequestProfile = vi.hoisted(() => vi.fn())
const unlockHusshOneVault = vi.fn()

vi.mock('@/hermes', () => ({
  connectHusshOne: () => connectHusshOne(),
  disconnectHusshOne: () => disconnectHusshOne(),
  enrollHusshOneVault: (profile?: string | null) => enrollHusshOneVault(profile),
  getHusshOneStatus: () => getHusshOneStatus(),
  lockHusshOneVault: () => lockHusshOneVault(),
  setApiRequestProfile: (...args: unknown[]) => setApiRequestProfile(...args),
  unlockHusshOneVault: () => unlockHusshOneVault()
}))

const disconnected = {
  authorization: { status: 'idle' as const },
  identity: { account_email: null, connected: false, device_id: null, environment: 'uat', profile_id: 'default' },
  onboarding: { remote_vault: 'not_connected' as const },
  vault: { connected: false, device_id: null, enrolled: false, profile_id: 'default', profile_locked: true, unlocked: false }
}

function renderSetup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <HusshOneSetup />
    </QueryClientProvider>
  )
}

describe('HusshOneSetup', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $activeGatewayProfile.set('default')
    getHusshOneStatus.mockResolvedValue(disconnected)
    connectHusshOne.mockResolvedValue({ authorization_url: 'https://example.test/connect', expires_in: 300, status: 'waiting' })
    enrollHusshOneVault.mockResolvedValue({ contract_compatible: true, enrolled: true, native_connector_ready: true, unlocked: true })
    lockHusshOneVault.mockResolvedValue({ locked: true })
    unlockHusshOneVault.mockResolvedValue({ unlocked: true })
    disconnectHusshOne.mockResolvedValue({ connected: false, revoked: true })
  })

  it('offers browser connection from persistent settings', async () => {
    renderSetup()

    expect(await screen.findByText('Not connected')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Connect in browser' })).toBeTruthy()
  })

  it('uses the native prompt bridge so the renderer never contains a vault passphrase field', async () => {
    getHusshOneStatus.mockResolvedValue({
      ...disconnected,
      identity: { ...disconnected.identity, account_email: 'owner@example.com', connected: true },
      vault: { ...disconnected.vault, connected: true }
    })
    renderSetup()

    await screen.findByRole('button', { name: 'Secure this device' })
    expect(screen.queryByLabelText('Hussh One vault passphrase')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Secure this device' }))

    await waitFor(() => expect(enrollHusshOneVault).toHaveBeenCalledWith('default'))
  })

  it('shows the verified account and first-vault action after browser approval', async () => {
    getHusshOneStatus.mockResolvedValue({
      ...disconnected,
      identity: { ...disconnected.identity, account_email: 'owner@example.com', connected: true },
      onboarding: { remote_vault: 'not_created' },
      vault: { ...disconnected.vault, connected: true }
    })
    renderSetup()

    expect(await screen.findByText('Create your vault')).toBeTruthy()
    expect(screen.getByText(/Connected as owner@example.com/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Create vault and secure this device' })).toBeTruthy()
  })

  it('does not call a dashboard-unlocked vault ready for chat PKM writes', () => {
    expect(
      husshOneStatusLabel({
      ...disconnected,
      identity: { ...disconnected.identity, account_email: 'owner@example.com', connected: true },
        vault: { ...disconnected.vault, connected: true, enrolled: true, unlocked: true }
      })
    ).toBe('Vault unlocked locally')
  })
})
