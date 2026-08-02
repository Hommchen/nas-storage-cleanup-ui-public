export const FILTERS = [
  { id: 'all', label: '全部资源' },
  { id: 'movie', label: '电影' },
  { id: 'tv', label: '电视剧' },
  { id: 'tv-incomplete', label: '不完整电视剧' },
  { id: 'library', label: '媒体库已入库' },
  { id: 'hr', label: 'H&R 保护中' },
  { id: 'review', label: '无做种限制' },
  { id: 'names', label: '名称待核' },
]

export const ACTIONS = {
  pause: {
    title: '停止做种',
    detail: '保留 qB 任务和全部文件',
  },
  retire: {
    title: '退出做种',
    detail: '移除 qB 任务，媒体仍保留',
  },
  delete: {
    title: '完整删除',
    detail: '移除任务、媒体和全部链接',
  },
}

export function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data') && response.success !== undefined) {
    return response.data
  }
  return response?.data ?? response
}

export function createLatestPlanApi(api) {
  let generation = 0
  let latestPlanResult = null

  return {
    ...api,
    get(...args) {
      return api.get(...args)
    },
    post(path, body, ...args) {
      if (!String(path || '').endsWith('/plan')) {
        return api.post(path, body, ...args)
      }

      const requestGeneration = ++generation
      let rawRequest
      try {
        rawRequest = Promise.resolve(api.post(path, body, ...args))
      } catch (error) {
        rawRequest = Promise.reject(error)
      }

      const result = (async () => {
        try {
          const response = await rawRequest
          if (requestGeneration !== generation && latestPlanResult) {
            return await latestPlanResult
          }
          return response
        } catch (error) {
          if (requestGeneration !== generation && latestPlanResult) {
            return await latestPlanResult
          }
          throw error
        }
      })()
      latestPlanResult = result
      return result
    },
  }
}

export function mediaType(item) {
  const type = String(item?.type || '').trim().toLowerCase()
  if (type === '电影' || type === 'movie') return 'movie'
  if (type === '电视剧' || type === 'tv' || type === 'series') return 'tv'

  // Keep older snapshots usable when the explicit type field is absent.
  const edition = String(item?.edition || '').trim().toLowerCase()
  if (edition === '电影' || edition.startsWith('电影 ·')) return 'movie'
  return ''
}

export function isIncompleteTv(item) {
  return mediaType(item) === 'tv' && item?.episodeIncomplete === true
}

export function matchesFilter(item, filter) {
  if (filter === 'all') return true
  if (filter === 'movie') return mediaType(item) === 'movie'
  if (filter === 'tv') return mediaType(item) === 'tv'
  if (filter === 'tv-incomplete') return isIncompleteTv(item)
  if (filter === 'library') return Boolean(item.library)
  if (filter === 'hr') return Boolean(item.hr || item.hrPending)
  if (filter === 'review') return !item.protected && item.qbSummary === '无 qB 任务'
  return item.metadataVerified === false
}

export function isDirectlyCleanable(item) {
  return !item.protected && item.qbSummary === '无 qB 任务'
}

export function filterResources(resources, { filter, search, safeOnly, descending }) {
  const query = String(search || '').trim().toLowerCase()
  return [...(resources || [])]
    .filter(item => {
      const text = `${item.title || ''} ${item.englishTitle || ''} ${item.edition || ''} ${item.siteSummary || ''}`.toLowerCase()
      return matchesFilter(item, filter) &&
        (!safeOnly || isDirectlyCleanable(item)) &&
        (!query || text.includes(query))
    })
    .sort((left, right) => {
      const order = descending ? Number(right.size || 0) - Number(left.size || 0) : Number(left.size || 0) - Number(right.size || 0)
      return order || String(left.title || '').localeCompare(String(right.title || ''), 'zh-CN')
    })
}

export function formatGiB(size) {
  const numeric = Number(size || 0)
  return numeric >= 1024 ? `${(numeric / 1024).toFixed(2)} TB` : `${numeric.toFixed(1)} GB`
}

export function formatBytes(size) {
  return formatGiB(Number(size || 0) / 1024 ** 3)
}

export function issueKey(issue, index) {
  return `${issue?.code || 'issue'}-${index}`
}
