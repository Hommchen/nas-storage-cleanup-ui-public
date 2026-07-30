<script setup>
defineProps({
  pluginId: { type: String, default: 'StorageCleanup' },
})
</script>

<template>
  <div class="config-note">
    <strong>存储清理已接入 MoviePilot</strong>
    <span>请从左侧“存储清理”进入完整页面。3000 端口页面继续保留。</span>
  </div>
</template>

<style scoped>
.config-note {
  display: grid;
  gap: 6px;
  padding: 20px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 12px;
}
.config-note span { opacity: .7; }
</style>
