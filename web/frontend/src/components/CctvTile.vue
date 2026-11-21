<script setup>
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import JMuxer from 'jmuxer'
import api from '../api'

const props = defineProps({
  instance: { type: Object, required: true },
})

const router = useRouter()
const videoEl = ref(null)
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
    flushingTime: 30,
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
      reconnectTimer = setTimeout(connectStream, 4000)
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

function openViewer() {
  router.push(`/viewer/${props.instance.id}`)
}
</script>

<template>
  <div class="cctv-tile" @click="openViewer">
    <div class="cctv-video-wrap">
      <video
        v-show="instance.running"
        ref="videoEl"
        class="cctv-video"
        muted
        autoplay
        playsinline
      />
      <div v-if="!instance.running" class="cctv-off">
        <span>Offline</span>
      </div>
    </div>

    <!-- Overlay Header -->
    <div class="cctv-overlay">
      <div class="cctv-header">
        <span class="live-dot" :class="{ 'live-dot--on': instance.running }" />
        <span class="cctv-name">{{ instance.name }}</span>
      </div>
      <div class="cctv-badges">
        <span v-if="instance.client_name" class="cctv-badge">
          {{ instance.client_name }}
        </span>
        <span class="cctv-badge" :class="{ 'cctv-badge--ok': instance.connection_status === 'connected' }">
          {{ instance.connection_status === 'connected' ? 'ADB' : 'OFF' }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cctv-tile {
  position: relative;
  background: #000;
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  cursor: pointer;
  aspect-ratio: 9 / 16;
  max-height: 380px;
  transition: all var(--transition);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}

.cctv-tile:hover {
  border-color: #10b981;
  transform: scale(1.02);
  box-shadow: 0 0 20px rgba(16, 185, 129, 0.3);
}

.cctv-video-wrap {
  width: 100%;
  height: 100%;
}

.cctv-video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cctv-off {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #71717a;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.cctv-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  padding: 8px 10px;
  background: linear-gradient(180deg, rgba(0, 0, 0, 0.8) 0%, transparent 100%);
  display: flex;
  align-items: center;
  justify-content: space-between;
  z-index: 10;
}

.cctv-header {
  display: flex;
  align-items: center;
  gap: 6px;
}

.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #71717a;
}

.live-dot--on {
  background: #10b981;
  box-shadow: 0 0 8px #10b981;
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0% { transform: scale(0.95); opacity: 0.8; }
  50% { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(0.95); opacity: 0.8; }
}

.cctv-name {
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}

.cctv-badges {
  display: flex;
  gap: 4px;
}

.cctv-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.15);
  color: #e4e4e7;
}

.cctv-badge--ok {
  background: rgba(16, 185, 129, 0.25);
  color: #34d399;
}
</style>
