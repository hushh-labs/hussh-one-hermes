import { atom, computed } from 'nanostores'

import { MOUSE_TRACKING } from '../config/env.js'
import { ZERO } from '../domain/usage.js'
import { DEFAULT_THEME } from '../theme.js'

import { DEFAULT_INDICATOR_STYLE, type UiState } from './interfaces.js'

const buildUiState = (): UiState => ({
  bgTasks: new Set(),
  busy: false,
  busyInputMode: 'queue',
  compact: false,
  detailsMode: 'collapsed',
  detailsModeCommandOverride: false,
  indicatorStyle: DEFAULT_INDICATOR_STYLE,
  info: null,
  liveSessionCount: 0,
  inlineDiffs: true,
  mouseTracking: MOUSE_TRACKING,
  notice: null,
  pasteCollapseLines: 5,
  pasteCollapseChars: 2000,
  sections: {},
  showCost: false,
  showReasoning: false,
  sid: null,
  status: 'summoning hermes…',
  statusBar: 'top',
  streaming: true,
  theme: DEFAULT_THEME,
  usage: ZERO
})

export const $uiState = atom<UiState>(buildUiState())

export const $uiTheme = computed($uiState, state => state.theme)
export const $uiSessionId = computed($uiState, state => state.sid)
// Authoritative live model for THIS session, sourced from the latest
// `session.info` event (same source the status bar renders). The model picker
// reads this so its popover can never disagree with the status line shown above
// the tool calls — even when an in-session switch (e.g. Claude→Vertex) lands
// before the picker's own `model.options` round-trip resolves.
export const $uiLiveModel = computed($uiState, state => state.info?.model ?? '')

export const getUiState = () => $uiState.get()

export const patchUiState = (next: Partial<UiState> | ((state: UiState) => UiState)) =>
  $uiState.set(typeof next === 'function' ? next($uiState.get()) : { ...$uiState.get(), ...next })

export const resetUiState = () => $uiState.set(buildUiState())
