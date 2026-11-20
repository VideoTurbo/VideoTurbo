import { createApp } from 'vue'
import App from './App.vue'
import { watchInstances } from './stores/cloud'
import { watchNotices } from './stores/notices'
import { startPolling } from './stores/status'
import './styles/base.css'

watchInstances()
watchNotices()
startPolling()
createApp(App).mount('#app')
