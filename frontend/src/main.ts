import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import ElementPlus from '@/plugins/element'
import { setupDirectives } from "@/directives";
import '@/styles/global.scss'

// 创建应用
const app = createApp(App)

// 使用插件
app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 挂载应用
setupDirectives(app)

app.mount('#app')
