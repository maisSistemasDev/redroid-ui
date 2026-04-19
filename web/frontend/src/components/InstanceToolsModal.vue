<script setup>
import { ref, onMounted } from 'vue'
import { useInstancesStore } from '../stores/instances'

const props = defineProps({ instance: Object })
const emit = defineEmits(['close'])
const store = useInstancesStore()

const activeTab = ref('gps')
const loading = ref(false)
const msg = ref('')

// GPS
const gpsLat = ref('')
const gpsLng = ref('')
const gpsActive = ref(false)

// Clipboard
const clipText = ref('')

// Files
const filePath = ref('/sdcard/')
const fileEntries = ref([])
const fileUploadRef = ref(null)

// Backup
const backupFile = ref(null)

// Terminal
const shellCmd = ref('')
const shellOutput = ref([])

onMounted(() => {
  if (props.instance?.fake_lat != null) {
    gpsLat.value = String(props.instance.fake_lat)
    gpsLng.value = String(props.instance.fake_lng)
    gpsActive.value = true
  }
})

function flash(text) {
  msg.value = text
  setTimeout(() => { msg.value = '' }, 3000)
}

async function setGps() {
  loading.value = true
  try {
    const lat = parseFloat(gpsLat.value)
    const lng = parseFloat(gpsLng.value)
    if (isNaN(lat) || isNaN(lng)) { flash('Coordenadas inválidas'); return }
    await store.updateGps(props.instance.id, { fake_lat: lat, fake_lng: lng })
    gpsActive.value = true
    flash('GPS definido')
  } catch { flash('Erro ao definir GPS') }
  finally { loading.value = false }
}

async function clearGps() {
  loading.value = true
  try {
    await store.updateGps(props.instance.id, { fake_lat: null, fake_lng: null })
    gpsLat.value = ''
    gpsLng.value = ''
    gpsActive.value = false
    flash('GPS removido')
  } catch { flash('Erro') }
  finally { loading.value = false }
}

async function pasteClipboard() {
  loading.value = true
  try {
    await store.setClipboard(props.instance.id, clipText.value)
    flash('Texto enviado ao clipboard')
  } catch { flash('Erro ao enviar clipboard') }
  finally { loading.value = false }
}

async function copyClipboard() {
  loading.value = true
  try {
    const text = await store.getClipboard(props.instance.id)
    clipText.value = text
    if (text) {
      try { await navigator.clipboard.writeText(text) } catch {}
      flash('Clipboard copiado')
    } else { flash('Clipboard vazio') }
  } catch { flash('Erro ao ler clipboard') }
  finally { loading.value = false }
}

async function loadFiles() {
  loading.value = true
  try {
    const data = await store.listFiles(props.instance.id, filePath.value)
    fileEntries.value = data?.entries || []
  } catch { flash('Erro ao listar arquivos') }
  finally { loading.value = false }
}

function navigateDir(name) {
  if (name === '..') {
    const parts = filePath.value.replace(/\/$/, '').split('/')
    parts.pop()
    filePath.value = (parts.join('/') || '/') + '/'
  } else {
    filePath.value = filePath.value + name + '/'
  }
  loadFiles()
}

async function uploadFile(e) {
  const file = e.target.files?.[0]
  if (!file) return
  loading.value = true
  try {
    await store.uploadFile(props.instance.id, file, filePath.value)
    flash(`${file.name} enviado`)
    loadFiles()
  } catch { flash('Erro no upload') }
  finally { loading.value = false; if (fileUploadRef.value) fileUploadRef.value.value = '' }
}

async function doBackup() {
  loading.value = true
  try {
    await store.downloadBackup(props.instance.id, props.instance.name)
    flash('Backup iniciado')
  } catch { flash('Erro no backup') }
  finally { loading.value = false }
}

async function doRestore(e) {
  const file = e.target.files?.[0]
  if (!file) return
  loading.value = true
  try {
    await store.restoreBackup(props.instance.id, file)
    flash('Restauração concluída')
  } catch (err) {
    flash(err.response?.data?.detail || 'Erro na restauração')
  }
  finally { loading.value = false }
}

async function runShell() {
  if (!shellCmd.value.trim()) return
  const cmd = shellCmd.value.trim()
  shellOutput.value.push({ cmd, stdout: '...', stderr: '' })
  shellCmd.value = ''
  try {
    const result = await store.adbShell(props.instance.id, cmd)
    shellOutput.value[shellOutput.value.length - 1] = { cmd, ...result }
  } catch {
    shellOutput.value[shellOutput.value.length - 1] = { cmd, stdout: '', stderr: 'Erro', returncode: -1 }
  }
}
</script>

<template>
  <Teleport to="body">
    <div class="overlay" @click.self="emit('close')">
      <div class="modal modal--tools">
        <div class="tools-header">
          <h2>Ferramentas — {{ instance.name }}</h2>
          <button class="btn btn--small" @click="emit('close')">&#10005;</button>
        </div>

        <div v-if="msg" class="tools-msg">{{ msg }}</div>

        <div class="tabs">
          <button v-for="t in ['gps','clipboard','files','backup','terminal']" :key="t"
                  :class="['tab', { active: activeTab === t }]"
                  @click="activeTab = t; if(t === 'files') loadFiles()">
            {{ {gps:'GPS',clipboard:'Clipboard',files:'Arquivos',backup:'Backup',terminal:'Terminal'}[t] }}
          </button>
        </div>

        <!-- GPS -->
        <div v-if="activeTab === 'gps'" class="tab-content">
          <div class="form-row">
            <input v-model="gpsLat" class="input input--half" placeholder="Latitude (-23.5505)" />
            <input v-model="gpsLng" class="input input--half" placeholder="Longitude (-46.6333)" />
          </div>
          <div class="form-actions">
            <button class="btn btn--primary" :disabled="loading" @click="setGps">Definir GPS</button>
            <button v-if="gpsActive" class="btn" :disabled="loading" @click="clearGps">Limpar</button>
          </div>
          <small class="hint">Injeta coordenadas mock via ADB. Funciona com Google Maps e apps que usam LocationManager.</small>
        </div>

        <!-- Clipboard -->
        <div v-if="activeTab === 'clipboard'" class="tab-content">
          <textarea v-model="clipText" class="input textarea" placeholder="Texto para enviar ao clipboard do Android" rows="4" />
          <div class="form-actions">
            <button class="btn btn--primary" :disabled="loading" @click="pasteClipboard">Enviar ao Android</button>
            <button class="btn" :disabled="loading" @click="copyClipboard">Copiar do Android</button>
          </div>
        </div>

        <!-- Files -->
        <div v-if="activeTab === 'files'" class="tab-content">
          <div class="form-row">
            <input v-model="filePath" class="input" @keydown.enter="loadFiles" />
            <button class="btn btn--small" @click="loadFiles">Ir</button>
          </div>
          <div class="file-list">
            <div class="file-entry file-entry--dir" @click="navigateDir('..')">&#128281; ..</div>
            <div v-for="f in fileEntries" :key="f.name"
                 :class="['file-entry', { 'file-entry--dir': f.is_dir }]"
                 @click="f.is_dir && navigateDir(f.name)">
              <span>{{ f.is_dir ? '&#128193;' : '&#128196;' }} {{ f.name }}</span>
              <small>{{ f.size }}</small>
            </div>
          </div>
          <div class="form-actions">
            <label class="btn btn--primary upload-label">
              Upload
              <input ref="fileUploadRef" type="file" hidden @change="uploadFile" />
            </label>
          </div>
        </div>

        <!-- Backup -->
        <div v-if="activeTab === 'backup'" class="tab-content">
          <p class="hint">Exporta /data da instância como .tar.gz. Para restaurar, a instância deve estar parada.</p>
          <div class="form-actions">
            <button class="btn btn--primary" :disabled="loading" @click="doBackup">
              <span v-if="loading" class="spinner" /> Baixar Backup
            </button>
            <label class="btn upload-label">
              Restaurar Backup
              <input type="file" accept=".tar.gz,.tgz" hidden @change="doRestore" />
            </label>
          </div>
        </div>

        <!-- Terminal -->
        <div v-if="activeTab === 'terminal'" class="tab-content">
          <div class="shell-output" ref="shellOutputEl">
            <div v-for="(entry, i) in shellOutput" :key="i" class="shell-entry">
              <div class="shell-cmd">$ {{ entry.cmd }}</div>
              <pre v-if="entry.stdout" class="shell-stdout">{{ entry.stdout }}</pre>
              <pre v-if="entry.stderr" class="shell-stderr">{{ entry.stderr }}</pre>
            </div>
          </div>
          <form class="form-row" @submit.prevent="runShell">
            <input v-model="shellCmd" class="input" placeholder="adb shell command..." autofocus />
            <button type="submit" class="btn btn--primary btn--small">Run</button>
          </form>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style>
.modal--tools {
  max-width: 560px;
  width: 95vw;
  max-height: 85vh;
  overflow-y: auto;
  animation: modal-in 0.25s ease both;
}
.tools-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.tools-header h2 { margin: 0; font-size: 16px; }
.tools-msg {
  background: var(--accent);
  color: #fff;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 10px;
  text-align: center;
}
.tabs {
  display: flex;
  gap: 2px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0;
}
.tab {
  background: none;
  border: none;
  color: var(--fg-muted);
  padding: 7px 12px;
  font-size: 13px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
}
.tab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.tab-content { min-height: 120px; }
.form-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.input--half { flex: 1; }
.textarea {
  width: 100%;
  resize: vertical;
  font-family: inherit;
}
.file-list {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 10px;
}
.file-entry {
  padding: 6px 10px;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
}
.file-entry:last-child { border-bottom: none; }
.file-entry--dir { cursor: pointer; }
.file-entry--dir:hover { background: rgba(100,100,255,0.06); }
.upload-label { cursor: pointer; }
.shell-output {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px;
  max-height: 300px;
  overflow-y: auto;
  margin-bottom: 10px;
  font-family: 'Fira Code', monospace;
  font-size: 12px;
}
.shell-cmd { color: var(--accent); margin-bottom: 2px; }
.shell-stdout { color: var(--fg); white-space: pre-wrap; word-break: break-all; margin: 0 0 8px; }
.shell-stderr { color: var(--danger); white-space: pre-wrap; word-break: break-all; margin: 0 0 8px; }
.shell-entry { margin-bottom: 6px; }

@keyframes modal-in {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
</style>
