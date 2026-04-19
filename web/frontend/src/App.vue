<script setup>
import { ref, computed } from 'vue'
import { useAuthStore } from './stores/auth'
import { useRouter, useRoute } from 'vue-router'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const sidebarOpen = ref(true)

const hideSidebar = computed(() => {
  return route.meta?.hideSidebar === true || !auth.isAuthenticated
})

function toggleSidebar() {
  sidebarOpen.value = !sidebarOpen.value
}

async function handleLogout() {
  auth.logout()
  router.push({ name: 'Login' })
}
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--no-sidebar': hideSidebar }">
    <aside
      v-if="!hideSidebar"
      class="sidebar"
      :class="{ 'sidebar--collapsed': !sidebarOpen }"
    >
      <div class="sidebar__header">
        <button class="sidebar__burger" @click="toggleSidebar" title="Menu">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <rect y="3" width="20" height="2" rx="1" fill="currentColor"/>
            <rect y="9" width="20" height="2" rx="1" fill="currentColor"/>
            <rect y="15" width="20" height="2" rx="1" fill="currentColor"/>
          </svg>
        </button>
        <transition name="fade">
          <span v-if="sidebarOpen" class="sidebar__logo">Virtuale</span>
        </transition>
      </div>

      <nav class="sidebar__nav">
        <router-link to="/dashboard" class="sidebar__link" active-class="sidebar__link--active" title="Instâncias">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
          </svg>
          <transition name="fade">
            <span v-if="sidebarOpen" class="sidebar__label">Instâncias</span>
          </transition>
        </router-link>
        <a
          class="sidebar__link"
          :class="{ 'sidebar__link--active': route.query.tab === 'images' }"
          title="Imagens"
          @click.prevent="router.push({ path: '/dashboard', query: { tab: 'images' } })"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="2" width="20" height="20" rx="5"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>
          </svg>
          <transition name="fade">
            <span v-if="sidebarOpen" class="sidebar__label">Imagens</span>
          </transition>
        </a>
      </nav>

      <div class="sidebar__footer">
        <div class="sidebar__user" v-if="auth.isAuthenticated">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>
          </svg>
          <transition name="fade">
            <span v-if="sidebarOpen" class="sidebar__user-email">{{ auth.user?.email }}</span>
          </transition>
        </div>
        <button class="sidebar__logout" @click="handleLogout" title="Sair">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
          <transition name="fade">
            <span v-if="sidebarOpen" class="sidebar__label">Sair</span>
          </transition>
        </button>
      </div>
    </aside>

    <main class="main-content">
      <router-view v-slot="{ Component }">
        <transition name="fade" mode="out-in">
          <component :is="Component" />
        </transition>
      </router-view>
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.app-shell--no-sidebar .main-content {
  width: 100%;
}

/* ── Sidebar ── */

.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  margin: 10px 0 10px 10px;
  border-radius: var(--radius-pill);
  padding: 14px 10px;
  transition: width var(--transition-slow), min-width var(--transition-slow);
  overflow: hidden;
  z-index: 10;
}

.sidebar--collapsed {
  width: var(--sidebar-collapsed);
  min-width: var(--sidebar-collapsed);
}

.sidebar__header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 6px;
  margin-bottom: 20px;
}

.sidebar__burger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  background: transparent;
  border: none;
  color: var(--fg-muted);
  border-radius: var(--radius);
  transition: color var(--transition), background var(--transition);
}

.sidebar__burger:hover {
  color: var(--accent);
  background: var(--bg-hover);
}

.sidebar__logo {
  font-size: 17px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: -0.3px;
  white-space: nowrap;
}

/* ── Nav links ── */

.sidebar__nav {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sidebar__link {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius-lg);
  color: var(--fg-muted);
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  transition: color var(--transition), background var(--transition);
  white-space: nowrap;
  cursor: pointer;
}

.sidebar__link:hover {
  color: var(--fg);
  background: var(--bg-hover);
}

.sidebar__link--active {
  color: var(--accent);
  background: var(--accent-glow);
}

.sidebar__link svg {
  flex-shrink: 0;
}

.sidebar__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Footer ── */

.sidebar__footer {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.sidebar__user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  color: var(--fg-muted);
  font-size: 12px;
}

.sidebar__user svg {
  flex-shrink: 0;
}

.sidebar__user-email {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidebar__logout {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: transparent;
  border: none;
  border-radius: var(--radius-lg);
  color: var(--fg-muted);
  font-size: 13px;
  font-weight: 500;
  transition: color var(--transition), background var(--transition);
  white-space: nowrap;
}

.sidebar__logout:hover {
  color: var(--danger);
  background: rgba(224, 82, 82, 0.08);
}

/* ── Main content ── */

.main-content {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: auto;
}

/* ── Responsive ── */

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    margin: 8px;
    z-index: 100;
    box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3);
  }

  .sidebar--collapsed {
    width: var(--sidebar-collapsed);
    min-width: var(--sidebar-collapsed);
  }
}
</style>
