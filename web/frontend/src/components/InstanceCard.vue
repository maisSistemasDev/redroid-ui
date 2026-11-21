<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useInstancesStore } from '../stores/instances'
import VpnSettingsModal from './VpnSettingsModal.vue'
import InstanceToolsModal from './InstanceToolsModal.vue'
import JMuxer from 'jmuxer'
import api from '../api'

const props = defineProps({
  instance: { type: Object, required: true },
})

const store = useInstancesStore()
const router = useRouter()
const actionLoading = ref('')
const apkInput = ref(null)
const videoInput = ref(null)
const videoEl = ref(null)
const showVpnModal = ref(false)
const showToolsModal = ref(false)
const showScreenshotModal = ref(false)
const screenshotData = ref('')
const rotateStatus = ref('')

const connectionLabel = computed(() => {
  const s = (props.instance.connection_status || 'unknown').toLowerCase()
  if (s === 'connected') return 'ADB OK'
  if (s === 'offline') return 'ADB OFFLINE'
  if (s === 'unauthorized') return 'ADB AUTH'
  if (s === 'disconnected') return 'ADB DOWN'
  return 'ADB ?'
})

const connectionBadgeClass = computed(() => {
  const s = (props.instance.connection_status || 'unknown').toLowerCase()
  if (s === 'connected') return 'badge--conn-up'
  if (s === 'offline' || s === 'unauthorized') return 'badge--conn-warn'
  return 'badge--conn-down'
})

const vpnLabel = computed(() => {
  const t = (props.instance.vpn_type || '').toLowerCase()
  if (t === 'tor') return 'TOR'
  if (t === 'wireguard') return 'WG'
  return 'VPN'
})

const vpnConnLabel = computed(() => {
  const s = (props.instance.vpn_connection_status || 'unknown').toLowerCase()
  if (s === 'connected') return `${vpnLabel.value} OK`
  if (s === 'starting') return `${vpnLabel.value} ...`
  if (s === 'disabled') return `${vpnLabel.value} OFF`
  return `${vpnLabel.value} DOWN`
})

const vpnConnBadgeClass = computed(() => {
  const s = (props.instance.vpn_connection_status || 'unknown').toLowerCase()
  if (s === 'connected') return 'badge--vpn-on'
  if (s === 'starting') return 'badge--vpn-warn'
  return 'badge--vpn-off'
})

let jmuxerInstance = null
let screenWs = null
let reconnectTimer = null
let viewerToken = ''

function wsBase() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}`
}

async function fetchToken() {
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/viewer_url/`)
    viewerToken = data.token || ''
  } catch {
    viewerToken = ''
  }
}

function connectStream() {
  if (!props.instance.running || !videoEl.value) return
  if (screenWs && screenWs.readyState <= WebSocket.OPEN) return

  jmuxerInstance = new JMuxer({
    node: videoEl.value,
    mode: 'video',
    flushingTime: 50,
    fps: 30,
    clearBuffer: true,
    debug: false,
  })

  const tokenParam = viewerToken ? `?token=${encodeURIComponent(viewerToken)}` : ''
  screenWs = new WebSocket(`${wsBase()}/ws/screen/${props.instance.id}/${tokenParam}`)
  screenWs.binaryType = 'arraybuffer'

  screenWs.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer && ev.data.byteLength > 0) {
      jmuxerInstance?.feed({ video: new Uint8Array(ev.data) })
    }
  }

  screenWs.onclose = () => {
    destroyStream()
    if (props.instance.running) {
      reconnectTimer = setTimeout(connectStream, 5000)
    }
  }

  screenWs.onerror = () => screenWs?.close()
}

function destroyStream() {
  clearTimeout(reconnectTimer)
  if (screenWs) { screenWs.onclose = null; screenWs.close(); screenWs = null }
  try { jmuxerInstance?.destroy() } catch {}
  jmuxerInstance = null
}

watch(() => props.instance.running, async (running) => {
  if (running) {
    await fetchToken()
    await nextTick()
    connectStream()
  } else {
    destroyStream()
  }
})

onMounted(async () => {
  if (props.instance.running) {
    await fetchToken()
    await nextTick()
    connectStream()
  }
})

onUnmounted(() => destroyStream())

async function doAction(action) {
  actionLoading.value = action
  try {
    if (action === 'start') await store.startInstance(props.instance.id)
    else if (action === 'stop') await store.stopInstance(props.instance.id)
    else if (action === 'delete') {
      if (confirm(`Remover "${props.instance.name}"?`)) {
        await store.deleteInstance(props.instance.id)
      }
    }
  } catch { /* handled by store */ }
  finally { actionLoading.value = '' }
}

function openFullscreen() {
  router.push(`/viewer/${props.instance.id}`)
}

async function handleApk(event) {
  const file = event.target.files?.[0]
  if (!file) return
  actionLoading.value = 'apk'
  try {
    await store.installApk(props.instance.id, file)
  } catch (err) {
    alert(err.response?.data?.detail || err.message || 'Erro ao instalar APK')
  } finally {
    actionLoading.value = ''
    if (apkInput.value) apkInput.value.value = ''
  }
}

async function handleInjectVideo(event) {
  const file = event.target.files?.[0]
  if (!file) return
  actionLoading.value = 'video'
  try {
    const formData = new FormData()
    formData.append('video', file)
    const { data } = await api.post(`/instances/${props.instance.id}/inject_video/`, formData)
    alert(data.message || 'Vídeo Remotion injetado com sucesso na Galeria!')
  } catch (err) {
    alert(err.response?.data?.detail || err.message || 'Erro ao injetar vídeo')
  } finally {
    actionLoading.value = ''
    if (videoInput.value) videoInput.value.value = ''
  }
}

async function captureScreenshot() {
  actionLoading.value = 'screenshot'
  try {
    const { data } = await api.get(`/instances/${props.instance.id}/screenshot/?encoding=base64`)
    screenshotData.value = `data:image/png;base64,${data.base64}`
    showScreenshotModal.value = true
  } catch (err) {
    alert('Erro ao capturar screenshot do dispositivo')
  } finally {
    actionLoading.value = ''
  }
}

async function rotateProxy() {
  actionLoading.value = 'rotate'
  try {
    const { data } = await api.post(`/instances/${props.instance.id}/rotate_proxy/`)
    rotateStatus.value = `Novo IP: ${data.new_ip}`
    setTimeout(() => { rotateStatus.value = '' }, 5000)
  } catch (err) {
    alert(err.response?.data?.detail || 'Erro ao rotacionar proxy móvel')
  } finally {
    actionLoading.value = ''
  }
}
</script>

<template>
  <div class="phone-card">
    <!-- Realistic Smartphone Chassis Mockup -->
    <div class="phone-chassis" @dblclick="openFullscreen">
      <div class="phone-notch" />
      <div class="phone-chassis__screen">
        <video
          v-show="instance.running"
          ref="videoEl"
          class="phone-video"
          muted
          autoplay
          playsinline
        />
        <div v-if="!instance.running" class="phone-off">
          <span class="off-icon">&#9211;</span>
          <span class="off-label">Dispositivo Desligado</span>
        </div>
      </div>
    </div>

    <!-- Device Tags and Status Indicators -->
    <div class="phone-info">
      <div class="phone-header-row">
        <span class="phone-name" :title="instance.name">{{ instance.name }}</span>
        <span :class="['badge', instance.running ? 'badge--running' : 'badge--stopped']">
          {{ instance.status }}
        </span>
      </div>

      <div class="phone-badges-row">
        <span v-if="instance.client_name || instance.client_id" class="badge badge--client" :title="`Cliente: ${instance.client_name || instance.client_id}`">
          🏢 {{ instance.client_name || instance.client_id }}
        </span>
        <span :class="['badge', connectionBadgeClass]" :title="`Conexão ADB: ${instance.connection_status || 'unknown'}`">
          {{ connectionLabel }}
        </span>
        <span v-if="instance.vpn_enabled" :class="['badge', vpnConnBadgeClass]" :title="`Status do túnel: ${instance.vpn_connection_status || 'unknown'}`">
          {{ vpnConnLabel }}
        </span>
        <span v-if="instance.device_profile" class="badge badge--profile" :title="`Perfil: ${instance.device_profile}`">
          {{ instance.device_profile }}
        </span>
        <span v-if="instance.proxy_rotate_url" class="badge badge--running" title="Proxy Móvel 4G/5G com Rotação">
          4G Rotativo
        </span>
      </div>
      <div v-if="rotateStatus" class="rotate-alert">{{ rotateStatus }}</div>
    </div>

    <!-- Quick Action Toolbar -->
    <div class="phone-actions">
      <button v-if="!instance.running" class="btn btn--small btn--primary" :disabled="!!actionLoading" @click="doAction('start')">
        <span v-if="actionLoading === 'start'" class="spinner" /> ▶ Ligar
      </button>
      <button v-if="instance.running" class="btn btn--small" :disabled="!!actionLoading" @click="doAction('stop')">
        <span v-if="actionLoading === 'stop'" class="spinner" /> ⏹ Parar
      </button>
      <button v-if="instance.running" class="btn btn--small btn--accent-outline" @click="openFullscreen" title="Abrir Console AI Split-Screen">
        ⚡ Viewer + AI
      </button>

      <!-- Instant Screenshot -->
      <button v-if="instance.running" class="btn btn--small" :disabled="!!actionLoading" @click="captureScreenshot" title="Capturar tela instantânea">
        <span v-if="actionLoading === 'screenshot'" class="spinner" /> 📸
      </button>

      <!-- Remotion Video Injector -->
      <label v-if="instance.running" class="btn btn--small apk-label" :class="{ disabled: !!actionLoading }" title="Injetar vídeo Remotion na Galeria">
        <span v-if="actionLoading === 'video'" class="spinner" /> 🎬 Galeria
        <input ref="videoInput" type="file" accept="video/mp4" hidden :disabled="!!actionLoading" @change="handleInjectVideo" />
      </label>

      <!-- 4G IP Rotator -->
      <button v-if="instance.proxy_rotate_url" class="btn btn--small" :disabled="!!actionLoading" @click="rotateProxy" title="Rotacionar IP Móvel 4G/5G">
        <span v-if="actionLoading === 'rotate'" class="spinner" /> 🔄 IP
      </button>

      <!-- Install APK -->
      <label class="btn btn--small apk-label" :class="{ disabled: !!actionLoading || !instance.running }" title="Instalar arquivo APK">
        <span v-if="actionLoading === 'apk'" class="spinner" /> APK
        <input ref="apkInput" type="file" accept=".apk" hidden :disabled="!!actionLoading || !instance.running" @change="handleApk" />
      </label>

      <button class="btn btn--small" :disabled="!!actionLoading" @click="showVpnModal = true" title="Configurar VPN / Tor">
        🔒 VPN
      </button>
      <button class="btn btn--small" :disabled="!!actionLoading" @click="showToolsModal = true" title="Configurações Avançadas e Shell">
        ⚙️
      </button>
      <button class="btn btn--small btn--danger" :disabled="!!actionLoading" @click="doAction('delete')" title="Excluir Instância">
        <span v-if="actionLoading === 'delete'" class="spinner" /> 🗑️
      </button>
    </div>

    <!-- Screenshot Modal Preview -->
    <div v-if="showScreenshotModal" class="overlay" @click.self="showScreenshotModal = false">
      <div class="modal" style="max-width: 380px; text-align: center;">
        <h2>📸 Captura de Tela (VLM Ready)</h2>
        <img :src="screenshotData" style="width: 100%; border-radius: 12px; margin-bottom: 14px; border: 1px solid var(--border);" />
        <div class="form-actions" style="justify-content: center;">
          <a :href="screenshotData" download="screenshot.png" class="btn btn--primary">Baixar PNG</a>
          <button class="btn" @click="showScreenshotModal = false">Fechar</button>
        </div>
      </div>
    </div>

    <VpnSettingsModal v-if="showVpnModal" :instance="instance" @close="showVpnModal = false" />
    <InstanceToolsModal v-if="showToolsModal" :instance="instance" @close="showToolsModal = false" />
  </div>
</template>

<style scoped>
.phone-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  background: rgba(18, 18, 21, 0.6);
  border: 1px solid var(--border);
  border-radius: 24px;
  padding: 16px;
  transition: all var(--transition);
  backdrop-filter: blur(10px);
}

.phone-card:hover {
  border-color: rgba(16, 185, 129, 0.4);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
}

.phone-video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.phone-off {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--fg-muted);
}

.off-icon {
  font-size: 32px;
  opacity: 0.3;
}

.off-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1px;
  opacity: 0.5;
}

.phone-info {
  width: 100%;
  max-width: 236px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.phone-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.phone-name {
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.phone-badges-row {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.rotate-alert {
  font-size: 11px;
  color: #10b981;
  background: rgba(16, 185, 129, 0.12);
  padding: 3px 6px;
  border-radius: 4px;
  text-align: center;
}

.phone-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 236px;
  flex-wrap: wrap;
  justify-content: center;
}

.apk-label {
  cursor: pointer;
}

.apk-label.disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
}
</style>
