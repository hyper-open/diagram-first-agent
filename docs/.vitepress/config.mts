import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'
import { sidebar } from './sidebar.mjs'

export default withMermaid(
  defineConfig({
    lang: 'zh-CN',
    title: 'Agent 学习手册',
    description: '从 ReAct 到 Agent 平台：架构分层、演进、网关、RAG、多 Agent、计费、内容安全与合规。',
    // GitHub Pages 项目站点部署在 /<repo>/ 子路径下
    base: '/diagram-first-agent/',
    cleanUrls: true,
    lastUpdated: true,
    head: [
      ['meta', { name: 'theme-color', content: '#4f46e5' }],
      ['meta', { property: 'og:type', content: 'website' }],
      ['meta', { property: 'og:site_name', content: 'diagram-first-agent' }],
      ['meta', { property: 'og:title', content: 'Agent 学习手册' }],
      ['meta', { property: 'og:description', content: '从零理解 AI Agent：架构、演进、技术与优化。' }],
      ['link', { rel: 'icon', href: '/logo.svg', type: 'image/svg+xml' }]
    ],
    themeConfig: {
      logo: '/logo.svg',
      search: { provider: 'local' },
      nav: [
        { text: '开始', link: '/start/roadmap' },
        { text: '架构主线', link: '/evolution/overview' },
        { text: '分层深入', link: '/layers/model' },
        { text: '实战', link: '/practice/mini-agent' },
        { text: '治理', link: '/layers/content-safety' },
        { text: '优化', link: '/advanced/optimization' }
      ],
      sidebar,
      outline: { level: [2, 3], label: '本页目录' },
      docFooter: { prev: '上一篇', next: '下一篇' },
      lastUpdated: { text: '最后更新于' },
      darkModeSwitchLabel: '外观',
      sidebarMenuLabel: '菜单',
      returnToTopLabel: '回到顶部',
      footer: {
        message: '从一次调用，到可运营的 Agent 平台。',
        copyright: 'diagram-first-agent · Agent 学习手册 · MIT License'
      }
    },
    mermaid: {
      theme: 'neutral',
      flowchart: { useMaxWidth: true },
      sequence: { useMaxWidth: true }
    },
    markdown: {
      lineNumbers: true
    }
  })
)
