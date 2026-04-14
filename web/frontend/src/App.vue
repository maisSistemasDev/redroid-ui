<script setup>
import { useAuthStore } from './stores/auth'
import { useRouter } from 'vue-router'

const auth = useAuthStore()
const router = useRouter()

async function handleLogout() {
  auth.logout()
  router.push({ name: 'Login' })
}
</script>

<template>
  <div class="app-layout">
    <nav class="navbar">
      <router-link to="/dashboard" class="navbar-brand">
        re-droid
      </router-link>
      <div class="navbar-spacer" />
      <template v-if="auth.isAuthenticated">
        <span class="navbar-user">{{ auth.user?.email }}</span>
        <button class="btn btn--small" @click="handleLogout">Sair</button>
      </template>
      <template v-else>
        <router-link to="/login" class="btn btn--small">Entrar</router-link>
        <router-link to="/register" class="btn btn--small">Cadastrar</router-link>
      </template>
    </nav>
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.navbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 24px;
  height: 52px;
  background: var(--bg-elevated);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.navbar-brand {
  font-size: 18px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: -0.5px;
}

.navbar-brand:hover {
  color: var(--accent-hover);
}

.navbar-spacer {
  flex: 1;
}

.navbar-user {
  font-size: 13px;
  color: var(--fg-muted);
}

.main-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: auto;
}
</style>
