export function refreshFeedback(elapsedSeconds, includeHitAndRun = true) {
  const elapsed = Math.max(0, Number(elapsedSeconds) || 0)
  if (elapsed < 5) return '正在读取 NAS 只读快照…'
  if (elapsed < 30) {
    return includeHitAndRun
      ? `正在核对媒体目录、qB 与 H&R（已等待 ${elapsed} 秒）`
      : `正在核对媒体目录与 qB（已等待 ${elapsed} 秒）`
  }
  return includeHitAndRun
    ? `远端 H&R 探测可能需要数分钟（已等待 ${elapsed} 秒），请保持页面打开。`
    : `远端资源核对可能需要数分钟（已等待 ${elapsed} 秒），请保持页面打开。`
}
