<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { useInstancesStore } from '../stores/instances'
import InstanceCard from '../components/InstanceCard.vue'
import CreateInstanceModal from '../components/CreateInstanceModal.vue'
import ImageBuilder from '../components/ImageBuilder.vue'

const store = useInstancesStore()
const route = useRoute()
const showCreate = ref(false)
const activeTab = ref('instances')
let pollTimer = null

watch(() => route.query.tab, (tab) => {
  if (tab === 'images') activeTab.value = 'images'
  else activeTab.value = 'instances'
}, { immediate: true })

onMounted(() => {
  store.fetchInstances()
  pollTimer = setInterval(() => store.fetchInstances(), 5000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="dashboard">
    <div class="dash-header">
      <div class="tab-bar">
        <button :class="['tab', { active: activeTab === 'instances' }]" @click="activeTab = 'instances'">
          Instâncias
        </button>
        <button :class="['tab', { active: activeTab === 'images' }]" @click="activeTab = 'images'">
          Imagens
        </button>
      </div>
      <button v-if="activeTab === 'instances'" class="btn btn--primary" @click="showCreate = true">
        + Nova Instância
      </button>
    </div>

    <transition name="fade" mode="out-in">
      <div v-if="activeTab === 'instances'" key="instances" class="tab-content">
        <div v-if="store.error" class="error-msg">{{ store.error }}</div>

        <div v-if="store.loading && !store.instances.length" class="empty-state">
          <span class="spinner" style="width: 28px; height: 28px" />
        </div>

        <div v-else-if="!store.instances.length" class="empty-state">
          <p>Nenhuma instância encontrada.</p>
          <button class="btn btn--primary" @click="showCreate = true">Criar primeira instância</button>
        </div>

        <div v-else class="phones-row">
          <InstanceCard
            v-for="inst in store.instances"
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
  padding: 24px 28px;
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
}

.tab-bar {
  display: flex;
  gap: 2px;
  background: var(--bg-surface);
  padding: 3px;
  border-radius: var(--radius-lg);
}

.tab {
  padding: 7px 18px;
  font-size: 13px;
  font-weight: 500;
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
  color: var(--accent);
  background: var(--bg-elevated);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.15);
}

.tab-content {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.phones-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  justify-content: center;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 20px;
  color: var(--fg-muted);
}
</style>
