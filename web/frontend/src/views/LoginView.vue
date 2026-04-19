<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const email = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function handleSubmit() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(email.value, password.value)
    const redirect = route.query.redirect || '/dashboard'
    router.push(redirect)
  } catch (err) {
    error.value =
      err.response?.data?.detail ||
      err.response?.data?.message ||
      'Erro ao fazer login. Verifique suas credenciais.'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <form class="auth-card" @submit.prevent="handleSubmit">
      <div class="auth-brand">Virtuale</div>
      <h1 class="auth-title">Entrar</h1>
      <p class="auth-subtitle">Acesse o painel de gerenciamento</p>

      <div v-if="error" class="error-msg">{{ error }}</div>

      <div class="form-group">
        <label class="label" for="email">E-mail</label>
        <input
          id="email"
          v-model="email"
          type="email"
          class="input"
          placeholder="seu@email.com"
          required
          autocomplete="email"
        />
      </div>

      <div class="form-group">
        <label class="label" for="password">Senha</label>
        <input
          id="password"
          v-model="password"
          type="password"
          class="input"
          placeholder="••••••••"
          required
          autocomplete="current-password"
        />
      </div>

      <button type="submit" class="btn btn--primary auth-submit" :disabled="loading">
        <span v-if="loading" class="spinner" />
        {{ loading ? 'Entrando...' : 'Entrar' }}
      </button>

      <p class="auth-footer">
        Não tem conta?
        <router-link to="/register">Criar conta</router-link>
      </p>
    </form>
  </div>
</template>

<style scoped>
.auth-page {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: radial-gradient(ellipse at 30% 20%, rgba(92, 184, 228, 0.06), transparent 60%),
              radial-gradient(ellipse at 70% 80%, rgba(92, 184, 228, 0.04), transparent 50%),
              var(--bg);
  min-height: 100vh;
}

.auth-card {
  width: 100%;
  max-width: 400px;
  background: rgba(26, 29, 33, 0.8);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border-hover);
  border-radius: var(--radius-pill);
  padding: 40px 36px;
}

.auth-brand {
  font-size: 13px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 20px;
}

.auth-title {
  font-size: 26px;
  font-weight: 700;
  color: var(--fg);
  margin-bottom: 4px;
}

.auth-subtitle {
  color: var(--fg-muted);
  font-size: 14px;
  margin-bottom: 28px;
}

.auth-submit {
  width: 100%;
  justify-content: center;
  padding: 12px;
  font-size: 14px;
  margin-top: 8px;
  border-radius: 12px;
}

.auth-footer {
  text-align: center;
  margin-top: 20px;
  font-size: 13px;
  color: var(--fg-muted);
}
</style>
