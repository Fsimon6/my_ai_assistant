<template>
  <img
    :src="placeholder"
    :data-src="src"
    :alt="alt"
    :class="['lazy-image', className]"
    @load="onLoad"
    ref="imgElement"
  />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted} from "vue"

const props = defineProps<{
  src: string
  alt?: string
  placeholder?: string
  className?: string
}>()

const emit = defineEmits<{
  loaded: []
}>()

const imgElement = ref<HTMLImageElement>()
const observer = ref<IntersectionObserver>()

const placeholder = props.placeholder
|| 'data:image/svg+xml;base64,' +
  'PHN2ZyB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgeG1sbnM9Imh0dHA6Ly93d' +
  '3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjE' +
  'wMCIgZmlsbD0iI2Y1ZjdmYSIvPjwvc3ZnPg=='

const loadImage = () => {
  if (imgElement.value && props.src) {
    imgElement.value.src = props.src
  }
}

const onLoad = () => {
  emit('loaded')
}

onMounted(() => {
  if ('IntersectionObserver' in window) {
    observer.value = new IntersectionObserver((entires) => {
      entires.forEach((entry) => {
        if (entry.isIntersecting) {
          loadImage()

          observer.value?.unobserve(entry.target)
        }
      })
    })

    if (imgElement.value) {
      observer.value.observe(imgElement.value)
    }
  } else {
    // 浏览器不支持IntersectionObserver，直接加载
    loadImage()
  }
})

onUnmounted(() => {
  if (observer.value) {
    observer.value.disconnect()
  }
})
</script>

<style scoped>
.lazy-image {
  opacity: 0;
  transition: opacity 0.3s ease;
}

.lazy-image.loaded {
  opacity: 1;
}
</style>
