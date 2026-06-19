// 应用入口：创建 app、挂载 router、全局引入 mnemo.css 设计系统。
import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './assets/mnemo.css'

createApp(App).use(router).mount('#app')
