<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useInstancesStore } from '../stores/instances'
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
        <span class="off-icon">⏻</span>
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
      <span v-if="instance.vpn_enabled" :class="['badge', instance.vpn_active ? 'badge--vpn-on' : 'badge--vpn-off']" :title="instance.vpn_active ? 'VPN conectada' : 'VPN desconectada'">
        VPN{{ instance.vpn_type === 'wireguard' ? ' WG' : '' }}
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
      <button class="btn btn--small btn--danger" :disabled="!!actionLoading" @click="doAction('delete')">
        <span v-if="actionLoading === 'delete'" class="spinner" /> ✕
      </button>
    </div>
  </div>
</template>

<style scoped>
.phone-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.phone-screen {
  width: 220px;
  height: 390px;
  border-radius: 18px;
  overflow: hidden;
  background: #000;
  border: 3px solid #333;
  cursor: pointer;
  position: relative;
  box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  transition: border-color var(--transition);
}

.phone-screen:hover {
  border-color: var(--accent);
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
  opacity: 0.4;
}

.off-label {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 1px;
  opacity: 0.5;
}

.phone-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 220px;
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

.badge--vpn-on { background: #1e7a3a; color: #fff; }
.badge--vpn-off { background: #6b4400; color: #fff; }
.badge--conn-up { background: #0f7a43; color: #fff; }
.badge--conn-warn { background: #8a5a00; color: #fff; }
.badge--conn-down { background: #6b2a2a; color: #fff; }
</style>
