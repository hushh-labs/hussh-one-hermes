// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  connectHusshOne,
  enrollHusshOneVault,
  getHusshOneStatus,
  unlockHusshOneVault
} from '@/hermes'

const STATUS_KEY = ['hussh-one-onboarding-status'] as const

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Hussh One setup could not be completed.'
}

export function HusshOneOnboarding() {
  const queryClient = useQueryClient()
  const [passphrase, setPassphrase] = useState('')

  const status = useQuery({
    queryKey: STATUS_KEY,
    queryFn: getHusshOneStatus,
    refetchInterval: query =>
      query.state.data?.authorization.status === 'waiting' ? 1_500 : false,
    retry: false
  })

  const connect = useMutation({
    mutationFn: () => connectHusshOne(),
    onSuccess: async result => {
      await window.hermesDesktop.openExternal(result.authorization_url)
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  })

  const enroll = useMutation({
    mutationFn: enrollHusshOneVault,
    onSettled: async () => {
      setPassphrase('')
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  })

  const unlock = useMutation({
    mutationFn: unlockHusshOneVault,
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    }
  })

  const current = status.data

  const pendingAuthorization = current?.authorization.status === 'waiting'

  const error =
    connect.error ||
    enroll.error ||
    unlock.error ||
    (current?.authorization.status === 'error'
      ? new Error(current.authorization.error || 'Hussh One authorization failed.')
      : null)

  return (
    <section className="grid w-full max-w-md gap-3 rounded-lg border border-(--stroke-nous) p-4 text-left">
      <div>
        <h3 className="text-sm font-semibold">Connect Hussh One</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Optional. Link this Hermes profile as a trusted device for consented PKM access and
          approved writes.
        </p>
      </div>

      {!current?.identity.connected && (
        <Button
          disabled={connect.isPending || pendingAuthorization}
          onClick={() => connect.mutate()}
          size="sm"
          variant="outline"
        >
          {pendingAuthorization ? 'Waiting for approval…' : 'Connect in browser'}
        </Button>
      )}

      {current?.identity.connected && !current.vault.enrolled && (
        <div className="grid gap-2">
          <p className="text-xs text-muted-foreground">
            Identity linked. Enter the vault passphrase once; it stays in this local setup
            process and is never sent to the agent.
          </p>
          <Input
            aria-label="Hussh One vault passphrase"
            autoComplete="off"
            onChange={event => setPassphrase(event.target.value)}
            placeholder="Vault passphrase"
            type="password"
            value={passphrase}
          />
          <Button
            disabled={!passphrase || enroll.isPending}
            onClick={() => enroll.mutate(passphrase)}
            size="sm"
          >
            {enroll.isPending ? 'Securing vault…' : 'Secure this device'}
          </Button>
        </div>
      )}

      {current?.vault.enrolled && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-emerald-600 dark:text-emerald-400">
            {current.vault.unlocked ? 'PKM bridge ready' : 'Vault secured and locked'}
          </p>
          {!current.vault.unlocked && (
            <Button
              disabled={unlock.isPending}
              onClick={() => unlock.mutate()}
              size="sm"
              variant="outline"
            >
              Unlock
            </Button>
          )}
        </div>
      )}

      {error && <p className="text-xs text-destructive">{errorMessage(error)}</p>}
    </section>
  )
}
