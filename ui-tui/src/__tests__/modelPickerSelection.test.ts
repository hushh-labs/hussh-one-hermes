import { describe, expect, it } from 'vitest'

import { resolvePickerSelection } from '../components/modelPicker.js'
import type { ModelOptionProvider } from '../gatewayTypes.js'

const prov = (over: Partial<ModelOptionProvider> & { name: string; slug: string }): ModelOptionProvider => ({
  models: [],
  ...over
})

describe('resolvePickerSelection — model popover opens in sync with the live model', () => {
  const providers: ModelOptionProvider[] = [
    prov({ name: 'Gemini', slug: 'gemini', models: ['gemini-3.5-flash', 'gemini-3.5-pro'] }),
    prov({
      name: 'Vertex Claude',
      slug: 'google-vertex-claude',
      is_current: true,
      models: ['claude-opus-4-8', 'claude-sonnet-4-6', 'claude-haiku-4-5']
    })
  ]

  it('lands on the current provider AND the active model within it', () => {
    // Regression: opening the popover used to highlight provider 0 / model 0
    // even though the session was on claude-opus-4-8 via Vertex.
    const sel = resolvePickerSelection(providers, 'claude-opus-4-8')
    expect(sel.providerIdx).toBe(1)
    expect(sel.modelIdx).toBe(0) // opus is index 0 within Vertex
  })

  it('lands on a non-first model within the current provider', () => {
    const sel = resolvePickerSelection(providers, 'claude-haiku-4-5')
    expect(sel.providerIdx).toBe(1)
    expect(sel.modelIdx).toBe(2) // haiku is index 2 within Vertex
  })

  it('falls back to model 0 when the live model is not in the provider list', () => {
    const sel = resolvePickerSelection(providers, 'some-unknown-model')
    expect(sel.providerIdx).toBe(1)
    expect(sel.modelIdx).toBe(0)
  })

  it('falls back to provider 0 when no provider is marked current', () => {
    const noCurrent = providers.map(p => ({ ...p, is_current: false }))
    const sel = resolvePickerSelection(noCurrent, 'gemini-3.5-pro')
    expect(sel.providerIdx).toBe(0)
    expect(sel.modelIdx).toBe(1) // gemini-3.5-pro is index 1 within Gemini
  })

  it('is safe on empty input', () => {
    expect(resolvePickerSelection([], '')).toEqual({ providerIdx: 0, modelIdx: 0 })
    expect(resolvePickerSelection([], 'whatever')).toEqual({ providerIdx: 0, modelIdx: 0 })
  })

  it('treats an empty live model as no preference (model 0)', () => {
    const sel = resolvePickerSelection(providers, '')
    expect(sel.providerIdx).toBe(1)
    expect(sel.modelIdx).toBe(0)
  })
})
