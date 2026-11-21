<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useInstancesStore } from '../stores/instances'
import InstanceCard from '../components/InstanceCard.vue'
import CctvTile from '../components/CctvTile.vue'
import CreateInstanceModal from '../components/CreateInstanceModal.vue'
import ImageBuilder from '../components/ImageBuilder.vue'

const store = useInstancesStore()
const route = useRoute()
const showCreate = ref(false)
const activeTab = ref('instances')
const viewMode = ref('cards') // 'cards' or 'cctv'
const searchQuery = ref('')
const selectedClient = ref('')
let pollTimer = null

watch(() => route.query.tab, (tab) => {
  if (tab === 'images') activeTab.value = 'images'
  else activeTab.value = 'instances'
}, { immediate: true })

onMounted(() => {
  store.fetchInstances()
  pollTimer = setInterval(() => store.fetchInstances(), 4000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

const totalCount = computed(() => store.instances.length)
const runningCount = computed(() => store.instances.filter(i => i.running).length)
const proxyCount = computed(() => store.instances.filter(i => i.vpn_enabled || i.proxy_rotate_url).length)

const availableClients = computed(() => {
  const set = new Set()
  store.instances.forEach(i => {
    if (i.client_name) set.add(i.client_name)
    else if (i.client_id) set.add(i.client_id)
  })
  return Array.from(set)
})

const filteredInstances = computed(() => {
  return store.instances.filter(i => {
    const matchesSearch = !searchQuery.value ||
      i.name.toLowerCase().includes(searchQuery.value.toLowerCase()) ||
      (i.client_name && i.client_name.toLowerCase().includes(searchQuery.value.toLowerCase())) ||
      (i.client_id && i.client_id.toLowerCase().includes(searchQuery.value.toLowerCase()))

    const matchesClient = !selectedClient.value ||
      i.client_name === selectedClient.value ||
      i.client_id === selectedClient.value

    return matchesSearch && matchesClient
  })
})
</script>

<template>
  <div class="dashboard">
    <!-- Top Header Bar -->
    <div class="dash-header">
      <div class="header-left">
        <div class="tab-bar">
          <button :class="['tab', { active: activeTab === 'instances' }]" @click="activeTab = 'instances'">
            📱 Dispositivos ({{ totalCount }})
          </button>
          <button :class="['tab', { active: activeTab === 'images' }]" @click="activeTab = 'images'">
            💾 Imagens Base
          </button>
        </div>

        <!-- Stats Overview Pills -->
        <div v-if="activeTab === 'instances'" class="stats-bar">
          <div class="stat-card">
            <span class="stat-card__val" style="color: #10b981;">{{ runningCount }}</span>
            <span class="stat-card__lbl">Online</span>
          </div>
          <div class="stat-card">
            <span class="stat-card__val" style="color: #60a5fa;">{{ proxyCount }}</span>
            <span class="stat-card__lbl">Proxies/VPN</span>
          </div>
        </div>
      </div>

      <div class="header-right">
        <!-- View Mode Switcher -->
        <div v-if="activeTab === 'instances'" class="view-toggle">
          <button
            class="toggle-btn"
            :class="{ active: viewMode === 'cards' }"
            title="Visualização em Cards Detalhados"
            @click="viewMode = 'cards'"
          >
            🗂️ Cards
          </button>
          <button
            class="toggle-btn"
            :class="{ active: viewMode === 'cctv' }"
            title="Mosaico Multi-Dispositivo CCTV"
            @click="viewMode = 'cctv'"
          >
            📺 Mosaico CCTV
          </button>
        </div>

        <button v-if="activeTab === 'instances'" class="btn btn--primary" @click="showCreate = true">
          + Novo Android
        </button>
      </div>
    </div>

    <!-- Filter & Search Toolbar -->
    <div v-if="activeTab === 'instances' && store.instances.length > 0" class="filter-toolbar">
      <input
        v-model="searchQuery"
        type="text"
        class="input search-input"
        placeholder="Buscar dispositivo ou cliente..."
      />

      <select v-if="availableClients.length > 0" v-model="selectedClient" class="input client-select">
        <option value="">Todos os Clientes</option>
        <option v-for="c in availableClients" :key="c" :value="c">{{ c }}</option>
      </select>
    </div>

    <!-- Content Area -->
    <transition name="fade" mode="out-in">
      <div v-if="activeTab === 'instances'" key="instances" class="tab-content">
        <div v-if="store.error" class="error-msg">{{ store.error }}</div>

        <div v-if="store.loading && !store.instances.length" class="empty-state">
          <span class="spinner" style="width: 32px; height: 32px" />
        </div>

        <div v-else-if="!store.instances.length" class="empty-state">
          <p>Nenhum dispositivo Android configurado ainda.</p>
          <button class="btn btn--primary" @click="showCreate = true">Criar primeiro dispositivo</button>
        </div>

        <!-- Mode 1: Detailed Cards View -->
        <div v-else-if="viewMode === 'cards'" class="phones-row">
          <InstanceCard
            v-for="inst in filteredInstances"
            :key="inst.id"
            :instance="inst"
          />
        </div>

        <!-- Mode 2: Multi-Device CCTV Grid View -->
        <div v-else-if="viewMode === 'cctv'" class="cctv-grid">
          <CctvTile
            v-for="inst in filteredInstances"
            :key="inst.id"
            :instance="inst"
          />
        </div>
      </div>

      <div v-else key="images" class="tab-content">
        <ImageBuilder />
      </div>
    </transition>

    <transition name="modal">
      <CreateInstanceModal v-if="showCreate" @close="showCreate = false" />
    </transition>
  </div>
</template>

<style scoped>
.dashboard {
  padding: 24px 32px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  height: 100%;
}

.dash-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.tab-bar {
  display: flex;
  gap: 3px;
  background: var(--bg-surface);
  padding: 4px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border);
}

.tab {
  padding: 7px 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg-muted);
  background: none;
  border: none;
  border-radius: 10px;
  cursor: pointer;
  transition: all var(--transition);
}

.tab:hover {
  color: var(--fg);
}

.tab.active {
  color: #fff;
  background: var(--bg-elevated);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}

.view-toggle {
  display: flex;
  background: var(--bg-surface);
  padding: 3px;
  border-radius: 10px;
  border: 1px solid var(--border);
}

.toggle-btn {
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-muted);
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: all var(--transition);
}

.toggle-btn.active {
  background: #27272a;
  color: #10b981;
}

.filter-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.search-input {
  max-width: 320px;
}

.client-select {
  max-width: 220px;
}

.tab-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.phones-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  justify-content: flex-start;
}

.cctv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
  padding-bottom: 24px;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 64px 20px;
  color: var(--fg-muted);
}
</style>
