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
const videoEl = ref(null)
const showVpnModal = ref(false)
const showToolsModal = ref(false)

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
</script>

<template>
  <div class="phone-card">
    <div class="phone-screen" @dblclick="openFullscreen">
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
        <span class="off-label">Desligado</span>
      </div>
    </div>

    <div class="phone-bar">
      <span class="phone-name" :title="instance.name">{{ instance.name }}</span>
      <span :class="['badge', instance.running ? 'badge--running' : 'badge--stopped']">
        {{ instance.status }}
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
      <span v-if="instance.magisk_enabled" class="badge badge--magisk" title="Magisk root ativo">
        Magisk
      </span>
    </div>

    <div class="phone-actions">
      <button v-if="!instance.running" class="btn btn--small" :disabled="!!actionLoading" @click="doAction('start')">
        <span v-if="actionLoading === 'start'" class="spinner" /> Iniciar
      </button>
      <button v-if="instance.running" class="btn btn--small" :disabled="!!actionLoading" @click="doAction('stop')">
        <span v-if="actionLoading === 'stop'" class="spinner" /> Parar
      </button>
      <button v-if="instance.running" class="btn btn--small btn--primary" @click="openFullscreen">
        Abrir
      </button>
      <label class="btn btn--small apk-label" :class="{ disabled: !!actionLoading || !instance.running }">
        <span v-if="actionLoading === 'apk'" class="spinner" /> APK
        <input ref="apkInput" type="file" accept=".apk" hidden :disabled="!!actionLoading || !instance.running" @change="handleApk" />
      </label>
      <button class="btn btn--small" :disabled="!!actionLoading" @click="showVpnModal = true" title="Configurar VPN / Tor">
        &#128274; VPN
      </button>
      <button class="btn btn--small" :disabled="!!actionLoading" @click="showToolsModal = true" title="GPS, Clipboard, Arquivos, Backup, Terminal">
        &#9881; Tools
      </button>
      <button class="btn btn--small btn--danger" :disabled="!!actionLoading" @click="doAction('delete')">
        <span v-if="actionLoading === 'delete'" class="spinner" /> &#10005;
      </button>
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
  gap: 10px;
  transition: transform var(--transition);
}

.phone-card:hover {
  transform: translateY(-2px);
}

.phone-screen {
  width: 220px;
  height: 390px;
  border-radius: 20px;
  overflow: hidden;
  background: #000;
  border: 2px solid var(--border-hover);
  cursor: pointer;
  position: relative;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35), 0 2px 8px rgba(0, 0, 0, 0.2);
  transition: border-color var(--transition), box-shadow var(--transition);
}

.phone-screen:hover {
  border-color: var(--accent);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35), 0 0 0 3px var(--accent-glow);
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
  letter-spacing: 1.5px;
  opacity: 0.4;
}

.phone-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  flex-wrap: wrap;
  justify-content: center;
}

.phone-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.phone-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: center;
}

.apk-label { cursor: pointer; }
.apk-label.disabled { opacity: 0.5; pointer-events: none; }

.badge--vpn-on {
  background: rgba(93, 186, 97, 0.14);
  color: var(--success);
}

.badge--vpn-off {
  background: rgba(139, 141, 144, 0.14);
  color: var(--fg-muted);
}

.badge--vpn-warn {
  background: rgba(232, 167, 62, 0.14);
  color: var(--warning);
}

.badge--conn-up {
  background: rgba(93, 186, 97, 0.14);
  color: var(--success);
}

.badge--conn-warn {
  background: rgba(232, 167, 62, 0.14);
  color: var(--warning);
}

.badge--conn-down {
  background: rgba(224, 82, 82, 0.14);
  color: var(--danger);
}

.badge--profile {
  background: rgba(130, 130, 220, 0.14);
  color: var(--accent);
  font-size: 10px;
  text-transform: uppercase;
}

.badge--magisk {
  background: rgba(50, 180, 130, 0.14);
  color: #32b482;
  font-size: 10px;
  text-transform: uppercase;
}
</style>
