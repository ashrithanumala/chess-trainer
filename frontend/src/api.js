const BASE = '/api'

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.status === 204 ? null : res.json()
}

export const api = {
  games: (params) => req('/games?' + new URLSearchParams(params)),
  game: (id) => req('/games/' + encodeURIComponent(id)),
  analyze: (id, depth = 16) =>
    req(`/games/${encodeURIComponent(id)}/analyze`, {
      method: 'POST',
      body: JSON.stringify({ depth }),
    }),
  explain: (id) => req(`/games/${encodeURIComponent(id)}/explain`, { method: 'POST' }),
  addNote: (id, body, ply = null) =>
    req(`/games/${encodeURIComponent(id)}/notes`, {
      method: 'POST',
      body: JSON.stringify({ body, ply }),
    }),
  deleteNote: (noteId) => req(`/notes/${noteId}`, { method: 'DELETE' }),
  addTag: (id, tag) =>
    req(`/games/${encodeURIComponent(id)}/tags`, {
      method: 'POST',
      body: JSON.stringify({ tag }),
    }),
  removeTag: (id, tag) =>
    req(`/games/${encodeURIComponent(id)}/tags/${encodeURIComponent(tag)}`, {
      method: 'DELETE',
    }),
  tags: () => req('/tags'),
  sync: (body) => req('/sync', { method: 'POST', body: JSON.stringify(body) }),
  jobs: () => req('/jobs'),
  engineStatus: () => req('/engine/status'),
  engineStop: () => req('/engine/stop', { method: 'POST' }),
  stopJob: () => req('/jobs/stop', { method: 'POST' }),
  analyzeBatch: (body) => req('/analyze/batch', { method: 'POST', body: JSON.stringify(body) }),
  repertoire: (params) => req('/repertoire?' + new URLSearchParams(params)),
  replies: (params) => req('/replies?' + new URLSearchParams(params)),
  ask: (body) => req('/ask', { method: 'POST', body: JSON.stringify(body) }),
  search: (q) => req('/search?' + new URLSearchParams({ q })),
  stats: () => req('/stats'),
  openings: (params) => req('/openings?' + new URLSearchParams(params)),
  evalFen: (fen, depth = 14, multipv = 1) =>
    req('/engine/eval', { method: 'POST', body: JSON.stringify({ fen, depth, multipv }) }),
  enginePlay: (fen, skill = 8, movetime_ms = 300) =>
    req('/engine/play', {
      method: 'POST',
      body: JSON.stringify({ fen, skill, movetime_ms }),
    }),
}
