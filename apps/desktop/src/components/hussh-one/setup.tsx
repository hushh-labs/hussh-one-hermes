// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import {
  connectHusshOne,
  disconnectHusshOne,
  enrollHusshOneVault,
  getHusshOneStatus,
  lockHusshOneVault,
  unlockHusshOneVault
} from '@/hermes'
import { Lock, RefreshCw } from '@/lib/icons'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { useStore } from '@nanostores/react'

export const HUSSH_ONE_STATUS_KEY = 'hussh-one-status'

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Hussh One setup could not be completed.'
}

export function husshOneStatusLabel(status: Awaited<ReturnType<typeof getHusshOneStatus>> | undefined): string {
  if (!status?.identity.connected) {
    return 'Not connected'
  }

  if (!status.vault.enrolled) {
    return status.onboarding?.remote_vault === 'not_created'
      ? 'Create your vault'
      : 'Vault setup needed'
  }

  return status.vault.unlocked ? 'Vault unlocked locally' : 'Vault locked'
}

/**
 * The only renderer for trusted-device setup. It is mounted in first-run
 * onboarding and Settings so an upgrade never hides this security workflow.
 */
export function HusshOneSetup({ compact = false }: { compact?: boolean }) {
  const queryClient = useQueryClient()
  const profile = normalizeProfileKey(useStore($activeGatewayProfile))
  const statusKey = [HUSSH_ONE_STATUS_KEY, profile] as const

  const status = useQuery({
    queryKey: statusKey,
    queryFn: getHusshOneStatus,
    refetchInterval: query => (query.state.data?.authorization.status === 'waiting' ? 1_500 : false),
    retry: false
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: statusKey })

  const connect = useMutation({
    mutationFn: () => connectHusshOne(),
    onSuccess: async result => {
      await window.hermesDesktop.openExternal(result.authorization_url)
      await refresh()
    }
  })

  const enroll = useMutation({
    mutationFn: () => enrollHusshOneVault(profile),
    onSettled: async () => {
      await refresh()
    }
  })

  const unlock = useMutation({ mutationFn: unlockHusshOneVault, onSettled: refresh })
  const lock = useMutation({ mutationFn: lockHusshOneVault, onSettled: refresh })
  const disconnect = useMutation({ mutationFn: disconnectHusshOne, onSettled: refresh })

  const current = status.data
  const remoteVault = current?.onboarding?.remote_vault ?? 'unavailable'
  const pendingAuthorization = current?.authorization.status === 'waiting'
  const error =
    connect.error ||
    enroll.error ||
    unlock.error ||
    lock.error ||
    disconnect.error ||
    (current?.authorization.status === 'error'
      ? new Error(current.authorization.error || 'Hussh One authorization failed.')
      : null)

  return (
    <section className={compact ? 'grid gap-3' : 'grid max-w-2xl gap-5'}>
      {!compact && (
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Hussh One</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Link this Hermes profile as a trusted device for consented PKM access and approved writes.
            </p>
          </div>
          <Button aria-label="Refresh Hussh One status" onClick={() => void refresh()} size="icon" type="button" variant="ghost">
            <RefreshCw className={status.isFetching ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
        </div>
      )}

      <div className="rounded-lg border border-(--stroke-nous) p-4">
        <div className="flex items-start gap-3">
          <Lock className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">{husshOneStatusLabel(current)}</p>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {current?.identity.connected
                ? `Connected as ${current.identity.account_email || 'an account that must reconnect to verify its email'} in ${current.identity.environment.toUpperCase()}.`
                : 'Not connected to Hussh One on this Hermes profile.'}
            </p>
            {current?.vault.unlocked && (
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                This confirms the local setup process only. Chat PKM writes also require the active gateway process to be unlocked.
              </p>
            )}
          </div>
        </div>

        {!current?.identity.connected && (
          <Button
            className="mt-4"
            disabled={connect.isPending || pendingAuthorization}
            onClick={() => connect.mutate()}
            size="sm"
            type="button"
            variant="outline"
          >
            {pendingAuthorization ? 'Waiting for approval…' : 'Connect in browser'}
          </Button>
        )}

        {current?.identity.connected && !current.vault.enrolled && (
          <div className="mt-4 grid gap-2">
            <p className="text-xs leading-5 text-muted-foreground">
              {remoteVault === 'not_created'
                ? 'This account does not have a vault yet. A protected native ceremony will create one, show the recovery key once, and secure this device without sending secrets to the renderer or agent.'
                : 'A protected native macOS prompt will collect the existing vault passphrase once. It is never sent to the renderer or agent.'}
            </p>
            <Button disabled={enroll.isPending} onClick={() => enroll.mutate()} size="sm" type="button">
              {enroll.isPending
                ? 'Securing vault…'
                : remoteVault === 'not_created'
                  ? 'Create vault and secure this device'
                  : 'Secure this device'}
            </Button>
          </div>
        )}

        {current?.identity.connected && (
          <div className="mt-4 flex flex-wrap gap-2">
            {current.vault.enrolled && !current.vault.unlocked ? (
              <Button disabled={unlock.isPending} onClick={() => unlock.mutate()} size="sm" type="button" variant="outline">
                {unlock.isPending ? 'Unlocking…' : 'Unlock'}
              </Button>
            ) : current.vault.enrolled ? (
              <Button disabled={lock.isPending} onClick={() => lock.mutate()} size="sm" type="button" variant="outline">
                Lock vault
              </Button>
            ) : null}
            <Button
              disabled={disconnect.isPending}
              onClick={() => disconnect.mutate()}
              size="sm"
              type="button"
              variant="ghost"
            >
              Disconnect
            </Button>
          </div>
        )}

        {error && <p className="mt-4 text-xs text-destructive">{errorMessage(error)}</p>}
      </div>
    </section>
  )
}
