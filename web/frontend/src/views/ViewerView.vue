<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import JMuxer from 'jmuxer'
import api from '../api'

const route = useRoute()
const router = useRouter()

const instanceId = route.params.id
const statusText = ref('Conectando...')
const showOverlay = ref(true)
const overlayText = ref('Conectando ao dispositivo...')
const clipboardText = ref('')

const videoEl = ref(null)
const viewerContainer = ref(null)
const hiddenInput = ref(null)

let jmuxerInstance = null
let screenWs = null
let inputWs = null
let mouseDown = false
let activeTouchId = null
let lastTouchTime = 0
let lastMoveTime = 0
let lastDownTime = 0
let lastInputTime = 0
const MOVE_THROTTLE_MS = 10
const DOWN_DEBOUNCE_MS = 35
const INPUT_DEBOUNCE_MS = 5
const SYNTHETIC_MOUSE_DELAY_MS = 700

const KEY_MAP = {
  Backspace: 67,
  Enter: 66,
  Escape: 111,
  Delete: 112,
  Home: 3,
  End: 123,
  PageUp: 92,
  PageDown: 93,
  ArrowUp: 19,
  ArrowDown: 20,
  ArrowLeft: 21,
  ArrowRight: 22,
  Tab: 61,
}

let viewerToken = ''
let reconnectScreenTimer = null
let reconnectInputTimer = null
let screenReconnectDelay = 2000
let inputReconnectDelay = 2000
const MAX_RECONNECT_DELAY = 10000

async function refreshToken() {
  try {
    const { data } = await api.get(`/instances/${instanceId}/viewer_url/`)
    viewerToken = data.token || ''
  } catch {
    viewerToken = ''
  }
}

function wsBase() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}`
}

function wsSend(obj) {
  if (!inputWs || inputWs.readyState === WebSocket.CLOSED) {
    connectInput()
  }
  if (!inputWs || inputWs.readyState !== WebSocket.OPEN) return
  inputWs.send(JSON.stringify(obj))
}

function sendKey(keycode) {
  wsSend({ type: 'key', keycode })
}

function sendTextChunks(raw) {
  const normalized = String(raw || '')
    .replace(/\r?\n/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return
  const chunkSize = 180
  for (let i = 0; i < normalized.length; i += chunkSize) {
    wsSend({ type: 'text', text: normalized.slice(i, i + chunkSize) })
  }
}

function sendKeyCombo(keycode) {
  // Send a modifier+key combination (e.g. Ctrl+A, Ctrl+C).
  // The backend handles this as "input keycombination <modifier> <key>".
  wsSend({ type: 'keycombo', modifier: 113, keycode })
}

async function pasteFromClipboard() {
  let text = ''
  try {
    text = await navigator.clipboard.readText()
  } catch {
    text = clipboardText.value
  }
  if (!text) return
  sendTextChunks(text)
}

function sendManualClipboard() {
  if (!clipboardText.value) return
  sendTextChunks(clipboardText.value)
}

function connectScreen() {
  if (screenWs && screenWs.readyState <= WebSocket.OPEN) return

  statusText.value = 'Conectando...'
  showOverlay.value = true
  overlayText.value = 'Conectando ao dispositivo...'

  const node = videoEl.value
  if (!node) return

  jmuxerInstance = new JMuxer({
    node,
    mode: 'video',
    flushingTime: 25,
    fps: 30,
    clearBuffer: true,
    debug: false,
    onReady: () => {
      statusText.value = 'Conectado'
      showOverlay.value = false
    },
    onError: () => {},
  })

  const tokenParam = viewerToken ? `?token=${encodeURIComponent(viewerToken)}` : ''
  screenWs = new WebSocket(`${wsBase()}/ws/screen/${instanceId}/${tokenParam}`)
  screenWs.binaryType = 'arraybuffer'

  screenWs.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      jmuxerInstance.feed({ video: new Uint8Array(ev.data) })
      if (showOverlay.value) {
        showOverlay.value = false
        statusText.value = 'Conectado'
      }
    }
  }

  screenWs.onopen = () => {
    statusText.value = 'Streaming...'
    screenReconnectDelay = 2000
    // Keep input channel healthy whenever screen reconnects.
    connectInput()
  }

  screenWs.onclose = () => {
    statusText.value = 'Desconectado'
    showOverlay.value = true
    const secs = Math.round(screenReconnectDelay / 1000)
    overlayText.value = `Conexão perdida. Reconectando em ${secs}s...`
    try { jmuxerInstance?.destroy() } catch {}
    jmuxerInstance = null
    reconnectScreenTimer = setTimeout(async () => {
      await refreshToken()
      connectScreen()
      connectInput()
    }, screenReconnectDelay)
    screenReconnectDelay = Math.min(screenReconnectDelay * 1.5, MAX_RECONNECT_DELAY)
  }

  screenWs.onerror = () => {
    screenWs?.close()
  }
}

function connectInput() {
  if (inputWs && inputWs.readyState <= WebSocket.OPEN) return

  const tokenParam = viewerToken ? `?token=${encodeURIComponent(viewerToken)}` : ''
  inputWs = new WebSocket(`${wsBase()}/ws/input/${instanceId}/${tokenParam}`)

  inputWs.onopen = () => {
    inputReconnectDelay = 2000
  }

  inputWs.onclose = () => {
    inputWs = null
    reconnectInputTimer = setTimeout(async () => {
      await refreshToken()
      connectInput()
    }, inputReconnectDelay)
    inputReconnectDelay = Math.min(inputReconnectDelay * 1.5, MAX_RECONNECT_DELAY)
  }

  inputWs.onerror = () => {
    inputWs?.close()
  }
}

function getVideoContentRect() {
  const video = videoEl.value
  if (!video) return null
  const elemRect = video.getBoundingClientRect()
  const vw = video.videoWidth || 720
  const vh = video.videoHeight || 1280
  const elemRatio = elemRect.width / elemRect.height
  const videoRatio = vw / vh

  let rw, rh, ox, oy
  if (videoRatio > elemRatio) {
    rw = elemRect.width
    rh = elemRect.width / videoRatio
    ox = 0
    oy = (elemRect.height - rh) / 2
  } else {
    rh = elemRect.height
    rw = elemRect.height * videoRatio
    ox = (elemRect.width - rw) / 2
    oy = 0
  }
  return { left: elemRect.left + ox, top: elemRect.top + oy, width: rw, height: rh }
}

function getCoords(e) {
  const rect = getVideoContentRect()
  if (!rect) return [0, 0]
  const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height))
  return [x, y]
}

function isInsideVideo(e) {
  const rect = getVideoContentRect()
  if (!rect) return false
  return e.clientX >= rect.left && e.clientX <= rect.left + rect.width &&
         e.clientY >= rect.top && e.clientY <= rect.top + rect.height
}

function startInteraction(e) {
  const now = performance.now()
  if (now - lastDownTime < DOWN_DEBOUNCE_MS) return false
  lastDownTime = now
  mouseDown = true
  const [x, y] = getCoords(e)
  wsSend({ type: 'down', x, y })
  return true
}

function moveInteraction(e) {
  if (!mouseDown) return
  const now = performance.now()
  if (now - lastMoveTime < MOVE_THROTTLE_MS) return
  lastMoveTime = now
  const [x, y] = getCoords(e)
  wsSend({ type: 'move', x, y })
}

function endInteraction() {
  if (!mouseDown) return
  mouseDown = false
  wsSend({ type: 'up' })
  // Keep hidden input focused to capture virtual keyboard
  // Use setTimeout to ensure it runs after current event processing
  setTimeout(() => hiddenInput.value?.focus(), 50)
}

function onPointerDown(e) {
  if (e.pointerType === 'mouse' && e.button !== 0) return
  if (!isInsideVideo(e)) return
  e.preventDefault()
  try {
    viewerContainer.value?.setPointerCapture(e.pointerId)
  } catch {
    // Ignore browsers that do not support pointer capture reliably.
  }
  startInteraction(e)
  // Focus hidden input to enable virtual keyboard on mobile/tablet
  nextTick(() => hiddenInput.value?.focus())
}

function onPointerMove(e) {
  e.preventDefault()
  moveInteraction(e)
}

function onPointerUp(e) {
  e.preventDefault()
  endInteraction()
}

function onPointerLeave() {
  endInteraction()
}

function onPointerCancel() {
  endInteraction()
}

function onMouseDown(e) {
  if (window.PointerEvent) return
  if (e.button !== 0) return
  if (!isInsideVideo(e)) return
  e.preventDefault()
  startInteraction(e)
}

function onMouseMove(e) {
  if (window.PointerEvent) return
  if (Date.now() - lastTouchTime < SYNTHETIC_MOUSE_DELAY_MS) return
  e.preventDefault()
  moveInteraction(e)
}

function onMouseUp(e) {
  if (window.PointerEvent) return
  if (Date.now() - lastTouchTime < SYNTHETIC_MOUSE_DELAY_MS) return
  e.preventDefault()
  endInteraction()
}

function onTouchStart(e) {
  if (window.PointerEvent) return
  const touch = e.changedTouches?.[0]
  if (!touch) return
  if (!isInsideVideo(touch)) return
  e.preventDefault()
  activeTouchId = touch.identifier
  lastTouchTime = Date.now()
  startInteraction(touch)
}

function onTouchMove(e) {
  if (window.PointerEvent) return
  if (!mouseDown || activeTouchId == null) return
  const touch = Array.from(e.changedTouches || []).find((t) => t.identifier === activeTouchId)
  if (!touch) return
  e.preventDefault()
  lastTouchTime = Date.now()
  moveInteraction(touch)
}

function onTouchEnd(e) {
  if (window.PointerEvent) return
  if (activeTouchId == null) return
  const touch = Array.from(e.changedTouches || []).find((t) => t.identifier === activeTouchId)
  if (!touch) return
  e.preventDefault()
  activeTouchId = null
  lastTouchTime = Date.now()
  endInteraction()
}

function onTouchCancel() {
  if (window.PointerEvent) return
  activeTouchId = null
  lastTouchTime = Date.now()
  endInteraction()
}

function onWheel(e) {
  if (!isInsideVideo(e)) return
  e.preventDefault()
  const [x, y] = getCoords(e)
  const dir = e.deltaY > 0 ? 'down' : 'up'
  wsSend({ type: 'scroll', x, y, dir })
}

function onKeyDown(e) {
  // Don't process key events if they came from the hidden input
  // (hidden input handles all text input now)
  if (e.target === hiddenInput.value) {
    return
  }

  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    const k = e.key.toLowerCase()
    if (k === 'v') {
      e.preventDefault()
      pasteFromClipboard()
      return
    }
    if (k === 'a') {
      e.preventDefault()
      sendKeyCombo(29) // KEYCODE_A
      return
    }
    if (k === 'c') {
      e.preventDefault()
      sendKeyCombo(31) // KEYCODE_C
      return
    }
    if (k === 'x') {
      e.preventDefault()
      sendKeyCombo(52) // KEYCODE_X
      return
    }
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
    e.preventDefault()
    pasteFromClipboard()
    return
  }
  if (KEY_MAP[e.key]) {
    e.preventDefault()
    sendKey(KEY_MAP[e.key])
  } else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
    e.preventDefault()
    // For single characters, prefer hidden input to capture them
    // This allows composition and IME events to work properly
    if (hiddenInput.value) {
      hiddenInput.value.value = e.key
      hiddenInput.value.dispatchEvent(new Event('input', { bubbles: true }))
      hiddenInput.value.value = ''
    } else {
      // Fallback if hidden input not available
      wsSend({ type: 'text', text: e.key })
    }
  }
}

function onHiddenInputChange(e) {
  // Capture virtual keyboard input (mobile/tablet)
  const text = e.target.value || ''
  if (!text) return

  // Debounce to prevent duplicate sends from both change and input events
  const now = Date.now()
  if (now - lastInputTime < INPUT_DEBOUNCE_MS) {
    e.target.value = ''
    return
  }
  lastInputTime = now

  // Send each character that was typed
  for (const char of text) {
    wsSend({ type: 'text', text: char })
  }

  // Clear the hidden input for next input
  e.target.value = ''
}

function onHiddenInputKeyDown(e) {
  // Handle special keys on hidden input
  if (KEY_MAP[e.key]) {
    e.preventDefault()
    sendKey(KEY_MAP[e.key])
    e.target.value = '' // Clear after sending key
    return
  }

  // Handle Backspace specially
  if (e.key === 'Backspace') {
    e.preventDefault()
    sendKey(67) // KEYCODE_DEL
    e.target.value = ''
    return
  }

  // Let normal characters be handled by oninput
  if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
    // Don't preventDefault - let the input field capture it naturally
    return
  }
}

function onContextMenu(e) {
  e.preventDefault()
}

function onDragStart(e) {
  e.preventDefault()
}

function cleanup() {
  clearTimeout(reconnectScreenTimer)
  clearTimeout(reconnectInputTimer)
  if (screenWs) {
    screenWs.onclose = null
    screenWs.close()
  }
  if (inputWs) {
    inputWs.onclose = null
    inputWs.close()
  }
  inputWs = null
  try { jmuxerInstance?.destroy() } catch {}
}

function goBack() {
  router.push('/dashboard')
}

onMounted(async () => {
  await refreshToken()
  await nextTick()
  connectScreen()
  connectInput()
  // Focus hidden input first to enable virtual keyboard capture from the start
  setTimeout(() => hiddenInput.value?.focus(), 100)
  viewerContainer.value?.focus()
})

onUnmounted(() => {
  cleanup()
})
</script>

<template>
  <div class="viewer-page">
    <div class="viewer-toolbar">
      <button class="toolbar-btn" title="Voltar ao painel" @click="goBack">
        ← Painel
      </button>
      <span class="toolbar-title">Virtuale: #{{ instanceId }}</span>
      <span class="toolbar-status">{{ statusText }}</span>
      <span class="toolbar-spacer" />
      <button class="toolbar-btn" title="Home" @click="sendKey(3)">Home</button>
      <button class="toolbar-btn" title="Voltar" @click="sendKey(4)">Back</button>
      <button class="toolbar-btn" title="Recentes" @click="sendKey(187)">Recents</button>
      <button class="toolbar-btn" title="Enter" @click="sendKey(66)">Enter</button>
      <button class="toolbar-btn" title="Tab" @click="sendKey(61)">Tab</button>
      <button class="toolbar-btn" title="Ctrl+A" @click="sendKeyCombo(29)">Ctrl+A</button>
      <button class="toolbar-btn" title="Ctrl+C" @click="sendKeyCombo(31)">Ctrl+C</button>
      <button class="toolbar-btn" title="Ctrl+X" @click="sendKeyCombo(52)">Ctrl+X</button>
      <button class="toolbar-btn" title="Colar clipboard local" @click="pasteFromClipboard">Colar</button>
    </div>

    <div
      ref="viewerContainer"
      class="viewer-container"
      tabindex="-1"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointerleave="onPointerLeave"
      @pointercancel="onPointerCancel"
      @wheel.prevent="onWheel"
      @keydown="onKeyDown"
      @contextmenu="onContextMenu"
      @dragstart="onDragStart"
      @mousedown="onMouseDown"
      @mousemove="onMouseMove"
      @mouseup="onMouseUp"
      @touchstart="onTouchStart"
      @touchmove="onTouchMove"
      @touchend="onTouchEnd"
      @touchcancel="onTouchCancel"
    >
      <!-- Hidden input to capture virtual keyboard (mobile/tablet) -->
      <input
        ref="hiddenInput"
        type="text"
        class="hidden-input"
        autocapitalize="off"
        autocorrect="off"
        autocomplete="off"
        spellcheck="false"
        @input="onHiddenInputChange"
        @keydown="onHiddenInputKeyDown"
        @change="onHiddenInputChange"
      />
      <div class="phone-frame">
        <video
          ref="videoEl"
          class="video-player"
          muted
          autoplay
          playsinline
        />
      </div>
      <div v-if="showOverlay" class="connecting-overlay">
        {{ overlayText }}
      </div>
    </div>
    <div class="clipboard-panel">
      <input
        v-model="clipboardText"
        class="clipboard-input"
        type="text"
        placeholder="Digite/cole texto e envie para o Android"
        @keydown.enter.prevent="sendManualClipboard"
      />
      <button class="toolbar-btn" @click="sendManualClipboard">Enviar texto</button>
    </div>
  </div>
</template>

<style scoped>
.viewer-page {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  background: var(--bg);
  overflow: hidden;
}

.viewer-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  font-size: 13px;
  overflow-x: auto;
}

.toolbar-title {
  font-weight: 600;
  color: var(--accent);
  white-space: nowrap;
}

.toolbar-status {
  color: var(--fg-muted);
  font-size: 12px;
  white-space: nowrap;
}

.toolbar-spacer {
  flex: 1;
}

.toolbar-btn {
  background: transparent;
  border: 1px solid var(--border-hover);
  color: var(--fg);
  padding: 4px 10px;
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  transition: all var(--transition);
}

.toolbar-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-glow);
}

.toolbar-btn:active {
  transform: scale(0.97);
}

.viewer-container {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  cursor: default;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  outline: none;
}

.phone-frame {
  position: relative;
  max-height: 100%;
  max-width: 100%;
  aspect-ratio: 9 / 16;
  height: 100%;
}

.video-player {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
  display: block;
  pointer-events: none;
}

.connecting-overlay {
  display: flex;
  align-items: center;
  justify-content: center;
  position: absolute;
  inset: 0;
  color: var(--fg);
  font-size: 15px;
  z-index: 3;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  pointer-events: none;
}

.clipboard-panel {
  display: flex;
  gap: 8px;
  padding: 8px 14px;
  border-top: 1px solid var(--border);
  background: var(--bg-surface);
}

.clipboard-input {
  flex: 1;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border-hover);
  border-radius: var(--radius);
  padding: 7px 10px;
  font-size: 13px;
  transition: border-color var(--transition), box-shadow var(--transition);
}

.clipboard-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-glow);
}

.hidden-input {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
  -webkit-user-select: none;
  user-select: none;
}
</style>
