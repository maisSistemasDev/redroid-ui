<script setup>
import { onMounted, ref } from 'vue'
import { useInstancesStore } from '../stores/instances'
import api from '../api'

const emit = defineEmits(['close'])
const store = useInstancesStore()

const name = ref('')
const androidVersion = ref('redroid/redroid:14.0.0-latest')
const useCustomImage = ref(false)
const customImage = ref('')
const availableImages = ref([])
const vpnEnabled = ref(false)
const vpnFile = ref(null)
const vpnConfig = ref('')
const vpnCaCert = ref('')
const vpnUsername = ref('')
const vpnPassword = ref('')
const error = ref('')
const loading = ref(false)

const versions = [
  { value: 'redroid/redroid:14.0.0-latest', label: 'Android 14' },
  { value: 'redroid/redroid:13.0.0-latest', label: 'Android 13' },
  { value: 'redroid/redroid:12.0.0_64only-latest', label: 'Android 12 (64-bit)' },
  { value: 'redroid/redroid:12.0.0-latest', label: 'Android 12' },
  { value: 'redroid/redroid:11.0.0-latest', label: 'Android 11' },
  { value: 'redroid/redroid:10.0.0-latest', label: 'Android 10' },
  { value: 'redroid/redroid:9.0.0-latest', label: 'Android 9' },
  { value: 'redroid/redroid:8.1.0-latest', label: 'Android 8.1' },
]

async function handleSubmit() {
  error.value = ''
  loading.value = true
  try {
    const image = useCustomImage.value
      ? customImage.value
      : androidVersion.value
    if (!image) {
      error.value = 'Selecione uma imagem customizada.'
      loading.value = false
      return
    }
    const payload = {
      name: name.value,
      image,
      gpu_mode: 'guest',
    }
    if (vpnEnabled.value && vpnConfig.value) {
      payload.vpn_enabled = true
      payload.vpn_config = vpnConfig.value
      if (vpnCaCert.value) payload.vpn_ca_cert = vpnCaCert.value
      if (vpnUsername.value) payload.vpn_username = vpnUsername.value
      if (vpnPassword.value) payload.vpn_password = vpnPassword.value
    }
    await store.createInstance(payload)
    emit('close')
  } catch (err) {
    error.value = err.response?.data?.detail || err.message || 'Erro ao criar instância.'
  } finally {
    loading.value = false
  }
}

async function fetchImages() {
  try {
    const { data } = await api.get('/images/')
    availableImages.value = data.images || []
    if (!customImage.value && availableImages.value.length) {
      customImage.value = availableImages.value[0].tag
    }
  } catch {
    availableImages.value = []
  }
}

function handleVpnFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (e) => { vpnConfig.value = e.target.result }
  reader.readAsText(file)
}

function handleCaCertFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (e) => { vpnCaCert.value = e.target.result }
  reader.readAsText(file)
}

onMounted(() => {
  fetchImages()
})
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="modal">
      <h2>Nova Instância</h2>

      <div v-if="error" class="error-msg">{{ error }}</div>

      <form @submit.prevent="handleSubmit">
        <div class="form-group">
          <label class="label" for="inst-name">Nome</label>
          <input id="inst-name" v-model="name" class="input" placeholder="meu-android" required />
        </div>

        <div class="form-group">
          <label class="label" for="inst-version">Versão Android</label>
          <select id="inst-version" v-model="androidVersion" class="input" :disabled="useCustomImage">
            <option v-for="v in versions" :key="v.value" :value="v.value">{{ v.label }}</option>
          </select>
        </div>

        <div class="form-group">
          <label class="label checkline">
            <input v-model="useCustomImage" type="checkbox" />
            Usar imagem customizada (Houdini/GApps/Spoof)
          </label>
        </div>

        <div v-if="useCustomImage" class="form-group">
          <label class="label" for="inst-image">Imagem disponível</label>
          <select id="inst-image" v-model="customImage" class="input" required>
            <option v-if="!availableImages.length" value="" disabled>Nenhuma imagem encontrada</option>
            <option v-for="img in availableImages" :key="img.id" :value="img.tag">{{ img.tag }}</option>
          </select>
        </div>

        <div class="form-group">
          <label class="label checkline">
            <input v-model="vpnEnabled" type="checkbox" />
            Usar VPN (OpenVPN)
          </label>
        </div>

        <template v-if="vpnEnabled">
          <div class="form-group">
            <label class="label" for="inst-vpn-file">Arquivo .ovpn</label>
            <input id="inst-vpn-file" ref="vpnFile" type="file" accept=".ovpn,.conf" class="input" @change="handleVpnFile" />
            <small v-if="vpnConfig" class="hint">Arquivo carregado ({{ vpnConfig.length }} bytes)</small>
          </div>
          <div class="form-group">
            <label class="label" for="inst-vpn-ca">Certificado CA (opcional)</label>
            <input id="inst-vpn-ca" type="file" accept=".crt,.pem,.cer" class="input" @change="handleCaCertFile" />
            <small class="hint">Necessário se o .ovpn referencia um arquivo CA externo (ex: TunnelBear)</small>
          </div>
          <div class="form-group">
            <label class="label" for="inst-vpn-user">Usuário VPN (opcional)</label>
            <input id="inst-vpn-user" v-model="vpnUsername" class="input" placeholder="username" />
          </div>
          <div class="form-group">
            <label class="label" for="inst-vpn-pass">Senha VPN (opcional)</label>
            <input id="inst-vpn-pass" v-model="vpnPassword" class="input" type="password" placeholder="password" />
          </div>
        </template>

        <div class="form-actions">
          <button type="button" class="btn" @click="emit('close')">Cancelar</button>
          <button type="submit" class="btn btn--primary" :disabled="loading">
            <span v-if="loading" class="spinner" />
            {{ loading ? 'Criando...' : 'Criar' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.checkline { display: flex; align-items: center; gap: 8px; }
</style>
