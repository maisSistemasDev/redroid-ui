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

// Agent Console Reactive States
const agentStatus = ref('Pronto')
const agentLogs = ref([
  { time: new Date().toLocaleTimeString(), text: 'Console do Agente inicializado. Aguardando comandos...', type: 'info' }
])
const lastScreenshot = ref('')
const lastScreenshotMs = ref(0)
const lastUiDump = ref('')
const isCapturing = ref(false)
const isDumpingUi = ref(false)
const isInjectingVideo = ref(false)
const isRotatingProxy = ref(false)
const manualTapX = ref('')
const manualTapY = ref('')
const manualText = ref('')
const videoInput = ref(null)

const videoEl = ref(null)
const viewerContainer = ref(null)
const hiddenInput = ref(null)

let jmuxerInstance = null
let screenWs = null
let inputWs = null
let mouseDown = false
let lastMoveTime = 0
let lastDownTime = 0
const MOVE_THROTTLE_MS = 10
const DOWN_DEBOUNCE_MS = 35

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

function addLog(text, type = 'info') {
  agentLogs.value.unshift({
    time: new Date().toLocaleTimeString(),
    text,
    type,
  })
  if (agentLogs.value.length > 100) agentLogs.value.pop()
}

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
  addLog(`Tecla enviada: keycode=${keycode}`, 'action')
}

function sendTextChunks(raw) {
  const normalized = String(raw || '').replace(/\r?\n/g, ' ').replace(/\s+/g, ' ').trim()
  if (!normalized) return
  const chunkSize = 180
  for (let i = 0; i < normalized.length; i += chunkSize) {
    wsSend({ type: 'text', text: normalized.slice(i, i + chunkSize) })
  }
  addLog(`Texto digitado: "${normalized.slice(0, 30)}${normalized.length > 30 ? '...' : ''}"`, 'action')
}

function sendManualClipboard() {
  if (!clipboardText.value) return
  sendTextChunks(clipboardText.value)
  clipboardText.value = ''
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
    statusText.value = 'Streaming Ativo'
    screenReconnectDelay = 2000
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

  screenWs.onerror = () => screenWs?.close()
}

function connectInput() {
  if (inputWs && inputWs.readyState <= WebSocket.OPEN) return
  const tokenParam = viewerToken ? `?token=${encodeURIComponent(viewerToken)}` : ''
  inputWs = new WebSocket(`${wsBase()}/ws/input/${instanceId}/${tokenParam}`)
  inputWs.onopen = () => { inputReconnectDelay = 2000 }
  inputWs.onclose = () => {
    inputWs = null
    reconnectInputTimer = setTimeout(async () => {
      await refreshToken()
      connectInput()
    }, inputReconnectDelay)
    inputReconnectDelay = Math.min(inputReconnectDelay * 1.5, MAX_RECONNECT_DELAY)
  }
  inputWs.onerror = () => inputWs?.close()
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
  setTimeout(() => hiddenInput.value?.focus(), 50)
}

function onPointerDown(e) {
  if (e.pointerType === 'mouse' && e.button !== 0) return
  if (!isInsideVideo(e)) return
  e.preventDefault()
  try { viewerContainer.value?.setPointerCapture(e.pointerId) } catch {}
  startInteraction(e)
  nextTick(() => hiddenInput.value?.focus())
}

function onPointerMove(e) { e.preventDefault(); moveInteraction(e) }
function onPointerUp(e) { e.preventDefault(); endInteraction() }
function onPointerLeave() { endInteraction() }
function onPointerCancel() { endInteraction() }

function onKeyDown(e) {
  if (e.target === hiddenInput.value) return
  if (e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
  if (KEY_MAP[e.key]) {
    e.preventDefault()
    sendKey(KEY_MAP[e.key])
  }
}

// ── AGENT ACTIONS VIA REST API GATEWAY ──
async function triggerVlmSnapshot() {
  isCapturing.value = true
  addLog('Capturando snapshot VLM via screencap...', 'action')
  try {
    const t0 = performance.now()
    const { data } = await api.get(`/instances/${instanceId}/screenshot/?encoding=base64`)
    const ms = Math.round(performance.now() - t0)
    lastScreenshot.value = `data:image/png;base64,${data.base64}`
    lastScreenshotMs.value = data.latency_ms || ms
    addLog(`Snapshot capturado em ${lastScreenshotMs.value}ms (${data.bytes} bytes)`, 'success')
  } catch (err) {
    addLog(`Falha no snapshot: ${err.message}`, 'error')
  } finally {
    isCapturing.value = false
  }
}

async function triggerUiDump() {
  isDumpingUi.value = true
  addLog('Dumping árvore UI Automator XML...', 'action')
  try {
    const { data } = await api.get(`/instances/${instanceId}/ui_dump/`)
    lastUiDump.value = data.xml || 'Sem hierarquia XML disponível'
    addLog(`UI Dump concluído (${lastUiDump.value.length} caracteres de nós)`, 'success')
  } catch (err) {
    addLog(`Falha no UI dump: ${err.message}`, 'error')
  } finally {
    isDumpingUi.value = false
  }
}

async function triggerManualTap() {
  if (!manualTapX.value || !manualTapY.value) return
  const x = parseInt(manualTapX.value)
  const y = parseInt(manualTapY.value)
  addLog(`Enviando tap raiz em (${x}, ${y})...`, 'action')
  try {
    const { data } = await api.post(`/instances/${instanceId}/tap/`, { x, y })
    addLog(`Tap executado: status=${data.status}`, 'success')
  } catch (err) {
    addLog(`Erro ao executar tap: ${err.message}`, 'error')
  }
}

async function triggerManualType() {
  if (!manualText.value) return
  const text = manualText.value
  addLog(`Enviando texto via API: "${text}"`, 'action')
  try {
    const { data } = await api.post(`/instances/${instanceId}/type/`, { text })
    addLog(`Texto injetado (${data.method})`, 'success')
    manualText.value = ''
  } catch (err) {
    addLog(`Erro ao digitar texto: ${err.message}`, 'error')
  }
}

async function triggerInjectVideoFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  isInjectingVideo.value = true
  addLog(`Injetando vídeo Remotion "${file.name}" na galeria...`, 'action')
  try {
    const formData = new FormData()
    formData.append('video', file)
    const { data } = await api.post(`/instances/${instanceId}/inject_video/`, formData)
    addLog(`Vídeo pronto na galeria: ${data.gallery_path}`, 'success')
  } catch (err) {
    addLog(`Falha ao injetar vídeo: ${err.message}`, 'error')
  } finally {
    isInjectingVideo.value = false
    if (videoInput.value) videoInput.value.value = ''
  }
}

async function triggerRotateProxy() {
  isRotatingProxy.value = true
  addLog('Disparando webhook de rotação de IP 4G/5G...', 'action')
  try {
    const { data } = await api.post(`/instances/${instanceId}/rotate_proxy/`)
    addLog(`IP Rotacionado com sucesso! Novo IP: ${data.new_ip}`, 'success')
  } catch (err) {
    addLog(`Erro na rotação do proxy: ${err.response?.data?.detail || err.message}`, 'error')
  } finally {
    isRotatingProxy.value = false
  }
}

// Playbooks rápidos
async function runPlaybookWarmup() {
  addLog('Iniciando Playbook: Aquecimento de Feed (Scroll suave + delays humanos)...', 'action')
  try {
    await api.post(`/instances/${instanceId}/swipe/`, { x1: 360, y1: 900, x2: 360, y2: 300, duration: 400 })
    addLog('Swipe 1 executado.', 'info')
    setTimeout(async () => {
      await api.post(`/instances/${instanceId}/swipe/`, { x1: 360, y1: 850, x2: 360, y2: 350, duration: 350 })
      addLog('Swipe 2 executado. Playbook concluído.', 'success')
    }, 2000)
  } catch (err) {
    addLog(`Erro no playbook: ${err.message}`, 'error')
  }
}

function goBack() {
  router.push('/dashboard')
}

onMounted(async () => {
  await refreshToken()
  await nextTick()
  connectScreen()
  connectInput()
  setTimeout(() => hiddenInput.value?.focus(), 100)
  viewerContainer.value?.focus()
})

onUnmounted(() => {
  clearTimeout(reconnectScreenTimer)
  clearTimeout(reconnectInputTimer)
  if (screenWs) { screenWs.onclose = null; screenWs.close() }
  if (inputWs) { inputWs.onclose = null; inputWs.close() }
  try { jmuxerInstance?.destroy() } catch {}
})
</script>

<template>
  <div class="split-viewer-page">
    <!-- Top Global Bar -->
    <header class="viewer-topbar">
      <div class="topbar-left">
        <button class="btn btn--small" @click="goBack">← Painel</button>
        <span class="instance-title">Android #{{ instanceId }}</span>
        <span class="badge" :class="statusText === 'Streaming Ativo' ? 'badge--running' : 'badge--stopped'">
          {{ statusText }}
        </span>
      </div>

      <div class="topbar-actions">
        <!-- Quick Hardware Navigation Keys -->
        <button class="nav-key" title="Voltar (Back)" @click="sendKey(4)">◀ Voltar</button>
        <button class="nav-key" title="Início (Home)" @click="sendKey(3)">● Início</button>
        <button class="nav-key" title="Recentes (Apps)" @click="sendKey(187)">■ Recentes</button>
        <button class="nav-key" title="Ligar/Desligar Tela" @click="sendKey(26)">⏻ Tela</button>
      </div>
    </header>

    <!-- Main Split-Screen Workspace -->
    <div class="split-workspace">
      <!-- LEFT PANE: Phone Stream & Touch -->
      <section class="phone-pane">
        <div class="phone-wrapper">
          <div class="phone-chassis">
            <div class="phone-notch" />
            <div
              ref="viewerContainer"
              class="phone-chassis__screen"
              tabindex="-1"
              @pointerdown="onPointerDown"
              @pointermove="onPointerMove"
              @pointerup="onPointerUp"
              @pointerleave="onPointerLeave"
              @pointercancel="onPointerCancel"
              @keydown="onKeyDown"
              @contextmenu.prevent
            >
              <input
                ref="hiddenInput"
                type="text"
                class="hidden-input"
                autocapitalize="off"
                autocorrect="off"
                autocomplete="off"
              />
              <video
                ref="videoEl"
                class="phone-video-stream"
                muted
                autoplay
                playsinline
              />
              <div v-if="showOverlay" class="connecting-overlay">
                {{ overlayText }}
              </div>
            </div>
          </div>

          <!-- Bottom Soft Controls -->
          <div class="bottom-tools">
            <input
              v-model="clipboardText"
              class="input clip-input"
              type="text"
              placeholder="Digitar texto rápido no Android..."
              @keydown.enter.prevent="sendManualClipboard"
            />
            <button class="btn btn--small btn--primary" @click="sendManualClipboard">Enviar</button>
          </div>
        </div>
      </section>

      <!-- RIGHT PANE: Autonomous AI Agent Live Console -->
      <section class="agent-pane">
        <!-- Agent Status Header -->
        <div class="agent-header">
          <div class="agent-title-row">
            <div class="agent-badge">
              <span class="live-pulse" />
              <span>Agente AI Autônomo: {{ agentStatus }}</span>
            </div>
            <span class="model-tag">Gateway: APInex VLM (Gemini 2.0 / Kimi K3)</span>
          </div>

          <!-- Fast Automation Playbook Bar -->
          <div class="playbook-toolbar">
            <button class="play-btn" :disabled="isCapturing" @click="triggerVlmSnapshot">
              <span v-if="isCapturing" class="spinner" /> 📸 Snapshot VLM
            </button>
            <button class="play-btn" :disabled="isDumpingUi" @click="triggerUiDump">
              <span v-if="isDumpingUi" class="spinner" /> 🔍 Inspecionar Nós XML
            </button>
            <label class="play-btn" :class="{ disabled: isInjectingVideo }">
              <span v-if="isInjectingVideo" class="spinner" /> 🎬 Injetar Remotion
              <input ref="videoInput" type="file" accept="video/mp4" hidden :disabled="isInjectingVideo" @change="triggerInjectVideoFile" />
            </label>
            <button class="play-btn" :disabled="isRotatingProxy" @click="triggerRotateProxy">
              <span v-if="isRotatingProxy" class="spinner" /> 🔄 Rotacionar 4G
            </button>
            <button class="play-btn play-btn--accent" @click="runPlaybookWarmup">
              ⚡ Warmup Feed
            </button>
          </div>
        </div>

        <!-- Vision Inspection Thumbnail & Coordinates Tester -->
        <div class="agent-toolbox">
          <!-- Screenshot Preview -->
          <div class="toolbox-card">
            <span class="card-title">👁️ Visão do Modelo (Último Frame Capturado)</span>
            <div class="vlm-preview-box">
              <img v-if="lastScreenshot" :src="lastScreenshot" class="vlm-img" />
              <div v-else class="vlm-placeholder">
                <span>Clique em "Snapshot VLM" para analisar a tela</span>
              </div>
            </div>
            <span v-if="lastScreenshotMs" class="latency-label">Latência screencap: {{ lastScreenshotMs }}ms</span>
          </div>

          <!-- Direct Coordinate & Text Dispatcher -->
          <div class="toolbox-card">
            <span class="card-title">🎯 Disparo Manual de Coordenadas</span>
            <div class="coord-row">
              <input v-model="manualTapX" type="number" class="input coord-input" placeholder="X (ex: 360)" />
              <input v-model="manualTapY" type="number" class="input coord-input" placeholder="Y (ex: 640)" />
              <button class="btn btn--small btn--primary" @click="triggerManualTap">Toque</button>
            </div>
            <div class="coord-row" style="margin-top: 8px;">
              <input v-model="manualText" type="text" class="input" placeholder="Injetar texto seguro..." @keydown.enter="triggerManualType" />
              <button class="btn btn--small" @click="triggerManualType">Digitar</button>
            </div>
          </div>
        </div>

        <!-- Real-Time Decision & Action Logs Console -->
        <div class="agent-logs-panel">
          <div class="logs-header">
            <span>Terminal de Ações & Decisões do Agente</span>
            <button class="btn btn--ghost btn--small" @click="agentLogs = []">Limpar</button>
          </div>
          <div class="logs-stream">
            <div v-for="(log, idx) in agentLogs" :key="idx" class="log-line" :class="`log-line--${log.type}`">
              <span class="log-time">{{ log.time }}</span>
              <span class="log-text">{{ log.text }}</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.split-viewer-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
  background: #09090b;
  overflow: hidden;
}

.viewer-topbar {
  height: 52px;
  background: #121215;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px;
  flex-shrink: 0;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.instance-title {
  font-weight: 700;
  font-size: 14px;
  color: #fff;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.nav-key {
  background: #18181b;
  border: 1px solid var(--border);
  color: #d4d4d8;
  padding: 5px 12px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
  transition: all var(--transition);
}

.nav-key:hover {
  background: #27272a;
  border-color: #10b981;
  color: #fff;
}

.split-workspace {
  flex: 1;
  display: flex;
  min-height: 0;
  overflow: hidden;
}

/* ── LEFT PANE: PHONE STREAM ── */
.phone-pane {
  flex: 1.1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: radial-gradient(circle at center, #141418 0%, #09090b 100%);
  border-right: 1px solid var(--border);
  padding: 16px;
  overflow: hidden;
}

.phone-wrapper {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  height: 100%;
  max-height: 820px;
}

.phone-wrapper .phone-chassis {
  width: auto;
  aspect-ratio: 9 / 16;
  height: calc(100% - 50px);
  max-height: 740px;
  cursor: default;
}

.phone-wrapper .phone-chassis:hover {
  transform: none;
}

.phone-video-stream {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
  display: block;
  pointer-events: none;
}

.connecting-overlay {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 14px;
  backdrop-filter: blur(4px);
  z-index: 5;
}

.bottom-tools {
  width: 100%;
  max-width: 400px;
  display: flex;
  gap: 8px;
}

.clip-input {
  flex: 1;
}

.hidden-input {
  position: absolute;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
}

/* ── RIGHT PANE: AGENT LIVE CONSOLE ── */
.agent-pane {
  flex: 1.3;
  display: flex;
  flex-direction: column;
  background: #0d0d10;
  overflow: hidden;
}

.agent-header {
  padding: 14px 18px;
  background: #141417;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.agent-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.agent-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
  font-size: 13px;
  color: #fafafa;
}

.live-pulse {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 10px #10b981;
  animation: pulse 1.8s infinite;
}

.model-tag {
  font-size: 11px;
  color: #a1a1aa;
  background: rgba(255, 255, 255, 0.05);
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--border);
}

.playbook-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.play-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: #1e1e24;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: #e4e4e7;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all var(--transition);
}

.play-btn:hover {
  background: #27272a;
  border-color: #10b981;
  color: #10b981;
}

.play-btn--accent {
  background: rgba(16, 185, 129, 0.15);
  border-color: rgba(16, 185, 129, 0.4);
  color: #34d399;
}

.play-btn--accent:hover {
  background: #10b981;
  color: #022c22;
}

.agent-toolbox {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--border);
}

.toolbox-card {
  background: #121215;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.card-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  color: #a1a1aa;
  letter-spacing: 0.5px;
}

.vlm-preview-box {
  width: 100%;
  height: 110px;
  background: #000;
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border);
}

.vlm-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.vlm-placeholder {
  color: #52525b;
  font-size: 11px;
  text-align: center;
  padding: 10px;
}

.latency-label {
  font-size: 10px;
  color: #10b981;
  text-align: right;
}

.coord-row {
  display: flex;
  gap: 6px;
}

.coord-input {
  width: 90px;
}

/* ── ACTION LOGS PANEL ── */
.agent-logs-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 12px 18px;
}

.logs-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 700;
  color: #71717a;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}

.logs-stream {
  flex: 1;
  background: #08080a;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.log-line {
  display: flex;
  gap: 10px;
  line-height: 1.4;
}

.log-time {
  color: #52525b;
  flex-shrink: 0;
}

.log-line--info .log-text { color: #a1a1aa; }
.log-line--action .log-text { color: #60a5fa; }
.log-line--success .log-text { color: #34d399; }
.log-line--error .log-text { color: #f87171; }
</style>
