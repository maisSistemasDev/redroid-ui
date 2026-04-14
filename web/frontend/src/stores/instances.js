import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

export const useInstancesStore = defineStore('instances', () => {
  const instances = ref([])
  const loading = ref(false)
  const error = ref(null)
  let jobsWs = null
  let jobsReconnectTimer = null
  const pendingJobs = new Map()

  function makeJobError(detail) {
    return {
      response: { data: { detail: detail || 'Falha no processamento da tarefa' } },
      message: detail || 'Falha no processamento da tarefa',
    }
  }

  function wsBase() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.host}`
  }

  function resolveJob(payload) {
    const jobId = payload?.job_id
    if (!jobId) return
    const waiter = pendingJobs.get(jobId)
    if (!waiter) return
    if (payload.status === 'success') {
      clearInterval(waiter.poll)
      clearTimeout(waiter.timer)
      pendingJobs.delete(jobId)
      waiter.resolve(payload)
      return
    }
    if (payload.status === 'error') {
      clearInterval(waiter.poll)
      clearTimeout(waiter.timer)
      pendingJobs.delete(jobId)
      waiter.reject(makeJobError(payload.detail))
    }
  }

  function connectJobsWs() {
    const token = localStorage.getItem('access_token')
    if (!token) return
    if (jobsWs && jobsWs.readyState <= WebSocket.OPEN) return
    clearTimeout(jobsReconnectTimer)
    jobsWs = new WebSocket(`${wsBase()}/ws/jobs/?token=${encodeURIComponent(token)}`)
    jobsWs.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data || '{}')
        resolveJob(payload)
      } catch {
        // ignore malformed payload
      }
    }
    jobsWs.onclose = () => {
      jobsWs = null
      if (pendingJobs.size > 0) {
        jobsReconnectTimer = setTimeout(connectJobsWs, 2000)
      }
    }
    jobsWs.onerror = () => jobsWs?.close()
  }

  function waitJob(jobId) {
    connectJobsWs()
    return new Promise((resolve, reject) => {
      const poll = setInterval(async () => {
        try {
          const { data } = await api.get(`/instances/jobs/${jobId}/`)
          if (data?.status === 'success') {
            clearInterval(poll)
            clearTimeout(timer)
            pendingJobs.delete(jobId)
            resolve(data)
          } else if (data?.status === 'error') {
            clearInterval(poll)
            clearTimeout(timer)
            pendingJobs.delete(jobId)
            reject(makeJobError(data?.detail))
          }
        } catch {
          // keep waiting, websocket may still resolve first
        }
      }, 3000)
      const timer = setTimeout(() => {
        clearInterval(poll)
        pendingJobs.delete(jobId)
        reject(makeJobError('Tempo limite da tarefa excedido'))
      }, 15 * 60 * 1000)
      pendingJobs.set(jobId, { resolve, reject, timer, poll })
    })
  }

  async function fetchInstances() {
    loading.value = true
    error.value = null
    try {
      const { data } = await api.get('/instances/')
      instances.value = Array.isArray(data) ? data : []
    } catch (err) {
      error.value = err.response?.data?.detail || 'Erro ao carregar instâncias'
    } finally {
      loading.value = false
    }
  }

  async function createInstance(payload) {
    const { data } = await api.post('/instances/', payload)
    if (data?.job_id) {
      const done = await waitJob(data.job_id)
      await fetchInstances()
      return done
    }
    await fetchInstances()
    return data
  }

  async function startInstance(id) {
    await api.post(`/instances/${id}/start/`)
    await fetchInstances()
  }

  async function stopInstance(id) {
    await api.post(`/instances/${id}/stop/`)
    await fetchInstances()
  }

  async function deleteInstance(id) {
    await api.delete(`/instances/${id}/`)
    try {
      await api.post('/instances/cleanup_storage/')
    } catch {
      // keep deletion UX even if orphan cleanup endpoint is unavailable
    }
    await fetchInstances()
  }

  async function installApk(id, file) {
    const formData = new FormData()
    formData.append('file', file)
    const { data } = await api.post(
      `/instances/${id}/install/`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 0 }
    )
    if (data?.job_id) {
      return waitJob(data.job_id)
    }
    return data
  }

  async function updateVpn(id, payload) {
    await api.put(`/instances/${id}/vpn/`, payload)
    await fetchInstances()
  }

  return {
    instances,
    loading,
    error,
    fetchInstances,
    createInstance,
    startInstance,
    stopInstance,
    deleteInstance,
    installApk,
    updateVpn,
  }
})
