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
const vpnType = ref('openvpn')
const vpnProvider = ref('custom')
const vpnFile = ref(null)
const vpnConfig = ref('')
const vpnCaCert = ref('')
const vpnUsername = ref('')
const vpnPassword = ref('')
const vpnCountry = ref('')
const error = ref('')
const loading = ref(false)

// Countries with good Tor exit node coverage
const TOR_COUNTRIES = [
  { code: '', label: 'Qualquer país (padrão)' },
  { code: 'US', label: '🇺🇸 Estados Unidos' },
  { code: 'DE', label: '🇩🇪 Alemanha' },
  { code: 'NL', label: '🇳🇱 Holanda' },
  { code: 'FR', label: '🇫🇷 França' },
  { code: 'GB', label: '🇬🇧 Reino Unido' },
  { code: 'SE', label: '🇸🇪 Suécia' },
  { code: 'AT', label: '🇦🇹 Áustria' },
  { code: 'CH', label: '🇨🇭 Suíça' },
  { code: 'CA', label: '🇨🇦 Canadá' },
  { code: 'LU', label: '🇱🇺 Luxemburgo' },
  { code: 'CZ', label: '🇨🇿 República Tcheca' },
  { code: 'RO', label: '🇷🇴 Romênia' },
  { code: 'FI', label: '🇫🇮 Finlândia' },
  { code: 'ES', label: '🇪🇸 Espanha' },
  { code: 'AU', label: '🇦🇺 Austrália' },
  { code: 'JP', label: '🇯🇵 Japão' },
  { code: 'SG', label: '🇸🇬 Singapura' },
  { code: 'BR', label: '🇧🇷 Brasil' },
  { code: 'PL', label: '🇵🇱 Polônia' },
]

const vpnProviders = [
  { value: 'custom', label: 'Custom (arquivo .ovpn / .conf)' },
  { value: 'airvpn', label: 'AirVPN' },
  { value: 'cyberghost', label: 'Cyberghost' },
  { value: 'expressvpn', label: 'ExpressVPN' },
  { value: 'fastestvpn', label: 'FastestVPN' },
  { value: 'ivpn', label: 'IVPN' },
  { value: 'mullvad', label: 'Mullvad (WireGuard)' },
  { value: 'nordvpn', label: 'NordVPN' },
  { value: 'private internet access', label: 'Private Internet Access' },
  { value: 'protonvpn', label: 'ProtonVPN' },
  { value: 'surfshark', label: 'Surfshark' },
  { value: 'windscribe', label: 'Windscribe' },
]

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
    if (vpnEnabled.value) {
      payload.vpn_enabled = true
      payload.vpn_type = vpnType.value
      payload.vpn_provider = vpnProvider.value
      if (vpnType.value === 'tor') {
        if (vpnCountry.value) payload.vpn_country = vpnCountry.value
      } else {
        if (vpnConfig.value) payload.vpn_config = vpnConfig.value
        if (vpnCaCert.value) payload.vpn_ca_cert = vpnCaCert.value
        if (vpnUsername.value) payload.vpn_username = vpnUsername.value
        if (vpnPassword.value) payload.vpn_password = vpnPassword.value
      }
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
            Usar VPN (OpenVPN / Tor)
          </label>
        </div>

        <template v-if="vpnEnabled">
          <div class="form-group">
            <label class="label" for="inst-vpn-provider">Provider</label>
            <select id="inst-vpn-provider" v-model="vpnProvider" class="input">
              <option v-for="p in vpnProviders" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
          </div>

          <div v-if="vpnProvider === 'custom'" class="form-group">
            <label class="label" for="inst-vpn-type">Tipo VPN</label>
            <select id="inst-vpn-type" v-model="vpnType" class="input">
              <option value="openvpn">OpenVPN (arquivo .ovpn)</option>
              <option value="tor">Tor — Whonix-like (anônimo, sem config)</option>
            </select>
          </div>

          <!-- Tor: country selector only -->
          <template v-if="vpnType === 'tor'">
            <div class="form-group">
              <label class="label" for="inst-vpn-country">País de saída (nó de exit Tor)</label>
              <select id="inst-vpn-country" v-model="vpnCountry" class="input">
                <option v-for="c in TOR_COUNTRIES" :key="c.code" :value="c.code">{{ c.label }}</option>
              </select>
              <small class="hint">A disponibilidade de nós por país pode variar. Sem garantia de IP específico.</small>
            </div>
          </template>

          <!-- OpenVPN: config file + credentials -->
          <template v-else-if="vpnProvider === 'custom'">
            <div class="form-group">
              <label class="label" for="inst-vpn-file">Arquivo de configuração (.ovpn / .conf)</label>
              <input id="inst-vpn-file" ref="vpnFile" type="file" accept=".ovpn,.conf" class="input" @change="handleVpnFile" />
              <small v-if="vpnConfig" class="hint">Arquivo carregado ({{ vpnConfig.length }} bytes)</small>
            </div>

            <div class="form-group">
              <label class="label" for="inst-vpn-ca">Certificado CA (.crt / .pem) — opcional</label>
              <input id="inst-vpn-ca" type="file" accept=".crt,.pem,.cer" class="input" @change="handleCaCertFile" />
              <small class="hint">Necessário se o .ovpn referencia um arquivo CA externo (ex: TunnelBear)</small>
              <small v-if="vpnCaCert" class="hint">CA carregado ({{ vpnCaCert.length }} bytes)</small>
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
