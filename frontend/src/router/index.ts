import {createRouter, createWebHistory} from 'vue-router'
import type { RouteRecordRaw } from "vue-router";
import KnowledgeBase from "@/views/knowledge/KnowledgeBase.vue";

// 导入组件
const Login = () => import('@/views/auth/Login.vue')
const Register = () => import('@/views/auth/Register.vue')
const Dashboard = () => import('@/views/dashboard/Dashboard.vue')
const Characters = () => import('@/views/characters/Characters.vue')
const CharacterDetail = () => import('@/views/characters/CharacterDetail.vue')
const Chat = () => import('@/views/chat/Chat.vue')
const HomeView = () => import('@/views/HomeView.vue')


// 路由配置
const routes: Array<RouteRecordRaw> = [
  {
    path: '/',
    redirect: '/dashboard'
  },
  {
    path: '/login',
    name: 'Login',
    component: Login,
    meta: {
      requiresAuth: false,
      showLayout: false,
      title: '登录'
    }
  },
  {
    path: '/register',
    name: 'Register',
    component: Register,
    meta: {
      requiresAuth: false,
      showLayout: false,
      title: '注册'
    }
  },
  // ======== 需要MainLayout 的路由========
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: Dashboard,
    meta: {
      requiresAuth: true,   // 标记需要认证
      showLayout: true,
      title: '控制面板'
    }
  },
  {
    path: '/characters',
    name: 'Characters',
    component: Characters,
    meta: {
      requiresAuth: true,   // 标记需要认证
      showLayout: true,
      title: 'AI角色管理'
    }
  },
  {
    path: '/characters/:id',
    name: 'CharacterDetail',
    component: CharacterDetail,
    meta: {
      requiresAuth: true,
      showLayout: true,
      title: '角色详情'
    }
  },
  {
    path: '/chat/:id',
    name: 'Chat',
    component: Chat,
    meta: {
      requiresAuth: true,
      showLayout: true,
      title: '对话'
    }
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: KnowledgeBase,
    meta: {
      requiresAuth: true,
      title: '知识库管理',
    }
  },
  // 404页面处理
  {
    path: '/:pathMatch(.*)*',
    redirect: '/login'
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

// 路由守卫 - 认证检查
router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('access_token')
  const requiresAuth = to.meta.requiresAuth || false

  console.log(`导航检查：${to.path}, 需要认证：${requiresAuth}, 有token：${!!token}`)

  // 设置页面标题
  if (to.meta.title) {
    document.title = `${to.meta.title} - My AI Assistant`
  }

  // 如果需要认证但没token，跳转到登录页
  if (requiresAuth && !token) {
    console.log('需要认证但未登录，重定向到登录页')
    // 只有当目标不是登录页时才重定向，避免循环
    if (to.path !== '/login') {
      next('/login')
    } else {
      next()    // 已在登录页，继续
    }
    return
  }

  // 如果已登录但访问登录/注册页
  if ((to.path === '/login' || to.path === '/register') && token) {
    console.log('已登录但访问登录页，重定向到面板')
    next({ path: '/dashboard' })
    return
  }

  // 其他情况正常导航
    next()
})

export default router


