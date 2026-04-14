<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import api from '../api'

const images = ref([])
const imagesLoading = ref(false)

const androidVersions = [
  { value: '14.0.0', label: 'Android 14' },
  { value: '13.0.0', label: 'Android 13' },
  { value: '12.0.0', label: 'Android 12' },
  { value: '12.0.0_64only', label: 'Android 12 (64-bit)' },
  { value: '11.0.0', label: 'Android 11' },
  { value: '10.0.0', label: 'Android 10' },
  { value: '9.0.0', label: 'Android 9' },
  { value: '8.1.0', label: 'Android 8.1' },
]

// MindTheGapps only supports 12+
const mindthegappsVersions = new Set([
  '14.0.0', '13.0.0', '13.0.0_64only', '12.0.0', '12.0.0_64only',
])

const buildVersion = ref('11.0.0')
const buildGappsVariant = ref('litegapps')
const buildHoudini = ref(false)
const buildAutoSpoof = ref(false)

// Filter GApps options based on selected Android version
const gappsOptions = computed(() => {
  const opts = [
    { value: 'none', label: 'Sem GApps' },
    { value: 'litegapps', label: 'LiteGApps (todas as versões)' },
  ]
  if (mindthegappsVersions.has(buildVersion.value)) {
    opts.push({ value: 'mindthegapps', label: 'MindTheGapps (Android 12+)' })
  }
  opts.push({ value: 'gapps', label: 'OpenGApps (pode falhar)' })
  return opts
})

// Reset variant if incompatible with selected version
function onVersionChange() {
  if (buildGappsVariant.value === 'mindthegapps' && !mindthegappsVersions.has(buildVersion.value)) {
    buildGappsVariant.value = 'litegapps'
  }
}
const buildCleanupIntermediate = ref(true)
const buildSpoofBaseImage = ref('')
const buildRunning = ref(false)
const buildLog = ref('')
const buildResult = ref(null)
const buildError = ref('')

const spoofBaseImage = ref('')
const spoofRunning = ref(false)
const spoofLog = ref('')
const spoofResult = ref(null)
const spoofError = ref('')

let buildPollTimer = null
let spoofPollTimer = null

async function fetchImages() {
  imagesLoading.value = true
  try {
    const { data } = await api.get('/images/')
    images.value = data.images || []
  } catch { /* noop */ }
  finally { imagesLoading.value = false }
}

async function deleteImage(imageId) {
  if (!confirm(`Remover imagem "${imageId}"?`)) return
  try {
    await api.delete(`/images/${encodeURIComponent(imageId)}/`)
    await fetchImages()
  } catch (err) {
    alert(err.response?.data?.detail || 'Erro ao remover imagem')
  }
}

async function startBuild() {
  buildError.value = ''
  buildResult.value = null
  buildLog.value = ''
  try {
    await api.post('/images/build/', {
      android_version: buildVersion.value,
      gapps_variant: buildGappsVariant.value,
      houdini: buildHoudini.value,
      auto_spoof: buildAutoSpoof.value,
      cleanup_intermediate: buildCleanupIntermediate.value,
      spoof_base_image: buildSpoofBaseImage.value || '',
    })
    buildRunning.value = true
    pollBuildStatus()
  } catch (err) {
    buildError.value = err.response?.data?.detail || 'Erro ao iniciar build'
  }
}

async function pollBuildStatus() {
  try {
    const { data } = await api.get('/images/build/status/')
    buildRunning.value = data.running
    buildLog.value = data.log || ''
    if (data.result) buildResult.value = data.result
    if (data.running) buildPollTimer = setTimeout(pollBuildStatus, 2000)
    else await fetchImages()
  } catch {
    buildPollTimer = setTimeout(pollBuildStatus, 3000)
  }
}

async function startSpoof() {
  spoofError.value = ''
  spoofResult.value = null
  spoofLog.value = ''
  if (!spoofBaseImage.value) { spoofError.value = 'Selecione uma imagem.'; return }
  try {
    await api.post('/images/spoof/', { base_image: spoofBaseImage.value })
    spoofRunning.value = true
    pollSpoofStatus()
  } catch (err) {
    spoofError.value = err.response?.data?.detail || 'Erro ao iniciar spoof'
  }
}

async function pollSpoofStatus() {
  try {
    const { data } = await api.get('/images/spoof/status/')
    spoofRunning.value = data.running
    spoofLog.value = data.log || ''
    if (data.result) spoofResult.value = data.result
    if (data.running) spoofPollTimer = setTimeout(pollSpoofStatus, 2000)
    else await fetchImages()
  } catch {
    spoofPollTimer = setTimeout(pollSpoofStatus, 3000)
  }
}

onMounted(() => fetchImages())
onUnmounted(() => { clearTimeout(buildPollTimer); clearTimeout(spoofPollTimer) })
</script>

<template>
  <div class="image-builder">
    <div class="card">
      <h3 class="subsection-title">Imagens Disponíveis</h3>
      <div v-if="imagesLoading" style="padding: 12px"><span class="spinner" /></div>
      <div v-else-if="!images.length" class="empty-note">Nenhuma imagem redroid encontrada.</div>
      <div v-else class="image-list">
        <div v-for="img in images" :key="img.id" class="image-row">
          <div class="image-info">
            <span class="image-tag">{{ img.tag }}</span>
            <span class="image-size">{{ img.size }}</span>
          </div>
          <button class="btn btn--small btn--danger" @click="deleteImage(img.tag)">Excluir</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h3 class="subsection-title">Construir Imagem</h3>
      <p class="build-hint">
        Build determinístico: com Auto-spoof ativo, o spoof usa a imagem recém-buildada (não a "mais recente" da lista).
      </p>

      <div v-if="buildError" class="error-msg">{{ buildError }}</div>
      <div v-if="buildResult" :class="['result-msg', buildResult.success ? 'result-success' : 'result-error']">
        {{ buildResult.message }}
      </div>

      <form @submit.prevent="startBuild" class="build-form">
        <select v-model="buildVersion" class="input" @change="onVersionChange">
          <option v-for="v in androidVersions" :key="v.value" :value="v.value">{{ v.label }}</option>
        </select>
        <select v-model="buildGappsVariant" class="input">
          <option v-for="opt in gappsOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </select>
        <label class="check-opt">
          <input v-model="buildHoudini" type="checkbox" />
          Houdini
        </label>
        <label class="check-opt">
          <input v-model="buildAutoSpoof" type="checkbox" />
          Auto-spoof
        </label>
        <label class="check-opt">
          <input v-model="buildCleanupIntermediate" type="checkbox" :disabled="!buildAutoSpoof" />
          Limpar imagem intermediária
        </label>
        <select v-model="buildSpoofBaseImage" class="input" :disabled="!buildAutoSpoof">
          <option value="">Auto (imagem recém-buildada)</option>
          <option v-for="img in images" :key="`base-${img.id}`" :value="img.tag">{{ img.tag }}</option>
        </select>
        <button type="submit" class="btn btn--primary" :disabled="buildRunning">
          <span v-if="buildRunning" class="spinner" />
          {{ buildRunning ? 'Construindo...' : 'Construir' }}
        </button>
      </form>

      <div v-if="buildLog" class="log-box" style="margin-top: 16px">{{ buildLog }}</div>
    </div>

    <div class="card">
      <h3 class="subsection-title">Spoof (Pixel 6)</h3>

      <div v-if="spoofError" class="error-msg">{{ spoofError }}</div>
      <div v-if="spoofResult" :class="['result-msg', spoofResult.success ? 'result-success' : 'result-error']">
        {{ spoofResult.message }}
      </div>

      <form @submit.prevent="startSpoof" class="build-form">
        <select v-model="spoofBaseImage" class="input">
          <option value="" disabled>Selecione uma imagem</option>
          <option v-for="img in images" :key="img.id" :value="img.tag">{{ img.tag }}</option>
        </select>
        <button type="submit" class="btn btn--primary" :disabled="spoofRunning || !spoofBaseImage">
          <span v-if="spoofRunning" class="spinner" />
          {{ spoofRunning ? 'Processando...' : 'Spoof' }}
        </button>
      </form>

      <div v-if="spoofLog" class="log-box" style="margin-top: 16px">{{ spoofLog }}</div>
    </div>
  </div>
</template>

<style scoped>
.image-builder { display: flex; flex-direction: column; gap: 16px; }
.subsection-title { font-size: 15px; font-weight: 600; color: var(--accent); margin-bottom: 12px; }
.build-hint { font-size: 12px; color: var(--fg-muted); margin-bottom: 12px; }

.build-form { display: flex; gap: 10px; align-items: center; }
.build-form .input { flex: 1; min-width: 150px; }
.check-opt { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--fg-muted); }

.image-list { display: flex; flex-direction: column; gap: 8px; }
.image-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; background: var(--bg); border-radius: var(--radius); border: 1px solid var(--border);
}
.image-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.image-tag { font-size: 13px; font-weight: 500; word-break: break-all; }
.image-size { font-size: 12px; color: var(--fg-muted); }
.empty-note { font-size: 13px; color: var(--fg-muted); padding: 8px 0; }

.result-msg { padding: 10px 14px; border-radius: var(--radius); font-size: 13px; margin-bottom: 12px; }
.result-success { background: rgba(102,187,106,0.1); border: 1px solid rgba(102,187,106,0.3); color: var(--success); }
.result-error { background: rgba(239,83,80,0.1); border: 1px solid rgba(239,83,80,0.3); color: var(--danger); }
</style>
