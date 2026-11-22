import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SlaRecomputeView from '@/views/SlaRecomputeView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/sla-recompute', name: 'sla-recompute', component: SlaRecomputeView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
