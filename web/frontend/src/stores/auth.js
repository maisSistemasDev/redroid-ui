import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(localStorage.getItem('access_token') || '')
  const refreshToken = ref(localStorage.getItem('refresh_token') || '')
  const user = ref(null)

  const isAuthenticated = computed(() => !!accessToken.value)

  function setTokens(access, refresh) {
    accessToken.value = access
    refreshToken.value = refresh
    localStorage.setItem('access_token', access)
    localStorage.setItem('refresh_token', refresh)
  }

  function clearTokens() {
    accessToken.value = ''
    refreshToken.value = ''
    user.value = null
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
  }

  async function login(email, password) {
    const { data } = await api.post('/auth/login/', { email, password })
    setTokens(data.access, data.refresh)
    await fetchMe()
    return data
  }

  async function register(email, password) {
    await api.post('/auth/register/', { email, password, password_confirm: password })
    return await login(email, password)
  }

  async function fetchMe() {
    try {
      const { data } = await api.get('/auth/me/')
      user.value = data
    } catch {
      /* noop */
    }
  }

  async function refreshAccessToken() {
    try {
      const { data } = await api.post('/auth/refresh/', {
        refresh: refreshToken.value,
      })
      setTokens(data.access, data.refresh || refreshToken.value)
      return data.access
    } catch {
      clearTokens()
      throw new Error('Sessão expirada')
    }
  }

  function logout() {
    clearTokens()
  }

  if (accessToken.value) {
    fetchMe()
  }

  return {
    accessToken,
    refreshToken,
    user,
    isAuthenticated,
    login,
    register,
    logout,
    fetchMe,
    refreshAccessToken,
  }
})
