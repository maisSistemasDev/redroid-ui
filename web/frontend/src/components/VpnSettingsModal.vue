<script setup>
import { ref, onMounted } from 'vue'
import { useInstancesStore } from '../stores/instances'

const props = defineProps({
  instance: { type: Object, required: true },
})
const emit = defineEmits(['close'])
const store = useInstancesStore()

const vpnEnabled = ref(false)
const vpnType = ref('openvpn')
const vpnProvider = ref('custom')
const vpnConfig = ref('')
const vpnCaCert = ref('')
const vpnUsername = ref('')
const vpnPassword = ref('')
const vpnCountry = ref('')
const error = ref('')
const loading = ref(false)

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

function loadFromInstance() {
  const i = props.instance
  vpnEnabled.value = !!i.vpn_enabled
  vpnType.value = i.vpn_type || 'openvpn'
  vpnProvider.value = i.vpn_provider || 'custom'
  vpnUsername.value = i.vpn_username || ''
  vpnCountry.value = i.vpn_country || ''
  vpnPassword.value = ''
  vpnConfig.value = ''
  vpnCaCert.value = ''
}

onMounted(loadFromInstance)

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

async function handleSubmit() {
  error.value = ''
  loading.value = true
  try {
    const payload = {
      vpn_enabled: vpnEnabled.value,
      vpn_type: vpnType.value,
      vpn_provider: vpnProvider.value,
    }
    if (vpnEnabled.value) {
      if (vpnType.value === 'tor') {
        if (vpnCountry.value) payload.vpn_country = vpnCountry.value
      } else {
        if (vpnConfig.value) payload.vpn_config = vpnConfig.value
        if (vpnCaCert.value) payload.vpn_ca_cert = vpnCaCert.value
        if (vpnUsername.value) payload.vpn_username = vpnUsername.value
        if (vpnPassword.value) payload.vpn_password = vpnPassword.value
      }
    }
    await store.updateVpn(props.instance.id, payload)
    emit('close')
  } catch (err) {
    error.value = err.response?.data?.detail || err.message || 'Erro ao atualizar VPN.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <Teleport to="body">
  <div class="overlay" @click.self="emit('close')">
    <div class="modal modal--vpn">
      <h2>Configurar VPN</h2>
      <p class="modal-sub">{{ instance.name }}</p>

      <div v-if="error" class="error-msg">{{ error }}</div>

      <form @submit.prevent="handleSubmit">
        <div class="form-group">
          <label class="label checkline">
            <input v-model="vpnEnabled" type="checkbox" />
            VPN ativa
          </label>
        </div>

        <template v-if="vpnEnabled">
          <div class="form-group">
            <label class="label" for="vpn-type">Tipo</label>
            <select id="vpn-type" v-model="vpnType" class="input">
              <option value="openvpn">OpenVPN (arquivo .ovpn)</option>
              <option value="tor">Tor (anônimo, sem config)</option>
            </select>
          </div>

          <template v-if="vpnType === 'tor'">
            <div class="form-group">
              <label class="label" for="vpn-country">País de saída</label>
              <select id="vpn-country" v-model="vpnCountry" class="input">
                <option v-for="c in TOR_COUNTRIES" :key="c.code" :value="c.code">{{ c.label }}</option>
              </select>
              <small class="hint">A disponibilidade de nós por país pode variar.</small>
            </div>
          </template>

          <template v-else>
            <div class="form-group">
              <label class="label" for="vpn-file">Arquivo de configuração (.ovpn / .conf)</label>
              <input id="vpn-file" type="file" accept=".ovpn,.conf" class="input" @change="handleVpnFile" />
              <small v-if="vpnConfig" class="hint">Arquivo carregado ({{ vpnConfig.length }} bytes)</small>
              <small v-else class="hint">Deixe vazio para manter a configuração atual.</small>
            </div>

            <div class="form-group">
              <label class="label" for="vpn-ca">Certificado CA (.crt / .pem) — opcional</label>
              <input id="vpn-ca" type="file" accept=".crt,.pem,.cer" class="input" @change="handleCaCertFile" />
              <small v-if="vpnCaCert" class="hint">CA carregado ({{ vpnCaCert.length }} bytes)</small>
            </div>

            <div class="form-group">
              <label class="label" for="vpn-user">Usuário VPN</label>
              <input id="vpn-user" v-model="vpnUsername" class="input" placeholder="username" />
            </div>
            <div class="form-group">
              <label class="label" for="vpn-pass">Senha VPN</label>
              <input id="vpn-pass" v-model="vpnPassword" class="input" type="password" placeholder="nova senha (vazio = manter)" />
            </div>
          </template>
        </template>

        <div v-if="instance.running && vpnEnabled" class="warn-msg">
          O sidecar VPN será reiniciado automaticamente.
        </div>

        <div class="form-actions">
          <button type="button" class="btn" @click="emit('close')">Cancelar</button>
          <button type="submit" class="btn btn--primary" :disabled="loading">
            <span v-if="loading" class="spinner" />
            {{ loading ? 'Salvando...' : 'Salvar' }}
          </button>
        </div>
      </form>
    </div>
  </div>
  </Teleport>
</template>

<style>
.modal--vpn {
  animation: modal-in 0.25s ease both;
}

@keyframes modal-in {
  from {
    opacity: 0;
    transform: scale(0.95) translateY(10px);
  }
  to {
    opacity: 1;
    transform: scale(1) translateY(0);
  }
}

.modal-sub {
  font-size: 13px;
  color: var(--fg-muted);
  margin: -8px 0 12px 0;
}

.checkline {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.hint {
  display: block;
  font-size: 12px;
  color: var(--fg-muted);
  margin-top: 4px;
  line-height: 1.5;
}

.warn-msg {
  font-size: 12px;
  color: var(--warning);
  background: rgba(232, 167, 62, 0.1);
  border-radius: 6px;
  padding: 8px 12px;
  margin-top: 4px;
}
</style>
