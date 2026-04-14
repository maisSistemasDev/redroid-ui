<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useInstancesStore } from '../stores/instances'
import InstanceCard from '../components/InstanceCard.vue'
import CreateInstanceModal from '../components/CreateInstanceModal.vue'
import ImageBuilder from '../components/ImageBuilder.vue'

const store = useInstancesStore()
const showCreate = ref(false)
const activeTab = ref('instances')
let pollTimer = null

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
    <div class="tab-bar">
      <button :class="['tab', { active: activeTab === 'instances' }]" @click="activeTab = 'instances'">
        Instâncias
      </button>
      <button :class="['tab', { active: activeTab === 'images' }]" @click="activeTab = 'images'">
        Imagens
      </button>
    </div>

    <div v-if="activeTab === 'instances'" class="tab-content">
      <div class="section-header">
        <button class="btn btn--primary" @click="showCreate = true">+ Nova Instância</button>
      </div>

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

    <div v-if="activeTab === 'images'" class="tab-content">
      <ImageBuilder />
    </div>

    <CreateInstanceModal v-if="showCreate" @close="showCreate = false" />
  </div>
</template>

<style scoped>
.dashboard {
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  height: 100%;
}

.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0;
}

.tab {
  padding: 8px 20px;
  font-size: 14px;
  font-weight: 500;
  color: var(--fg-muted);
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}

.tab:hover { color: var(--fg); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.tab-content {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.section-header {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
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
  padding: 40px;
  color: var(--fg-muted);
}
</style>
