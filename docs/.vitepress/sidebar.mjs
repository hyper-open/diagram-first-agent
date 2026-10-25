export const sidebar = [
  {
    text: '开始',
    items: [
      { text: '学习路线', link: '/start/roadmap' },
      { text: '什么是 Agent', link: '/start/what-is-agent' }
    ]
  },
  {
    text: '架构演进主线',
    items: [
      { text: '00 架构总览', link: '/evolution/overview' },
      { text: '01 ReAct，最小可用 Agent', link: '/evolution/react' },
      { text: '02 工具调用与注册', link: '/evolution/tool-calling' },
      { text: '03 模型网关与路由', link: '/evolution/model-gateway' },
      { text: '04 工具网关', link: '/evolution/tool-gateway' },
      { text: '05 RAG 与知识层', link: '/evolution/rag' },
      { text: '06 意图识别与路由', link: '/evolution/intent' },
      { text: '07 多 Agent 协作', link: '/evolution/multi-agent' },
      { text: '08 平台化与治理', link: '/evolution/platform' }
    ]
  },
  {
    text: '基础参考',
    collapsed: true,
    items: [
      { text: '核心循环', link: '/foundations/loop' },
      { text: '七层职责模型', link: '/foundations/architecture' },
      { text: '技术演进史', link: '/foundations/evolution' }
    ]
  },
  {
    text: '分层深入',
    collapsed: true,
    items: [
      { text: '模型层', link: '/layers/model' },
      { text: '模型适配', link: '/layers/model-adaptation' },
      { text: '多模态与实时交互', link: '/layers/multimodal' },
      { text: '上下文层', link: '/layers/context' },
      { text: '检索层', link: '/layers/retrieval' },
      { text: '工具层', link: '/layers/tools' },
      { text: '记忆层', link: '/layers/memory' },
      { text: '编排层', link: '/layers/orchestration' },
      { text: '运行时与沙箱', link: '/layers/runtime' },
      { text: '测试与验证', link: '/layers/testing' },
      { text: '评测与可观测性', link: '/layers/evaluation' },
      { text: '版本与发布', link: '/layers/release' },
      { text: '安全与权限', link: '/layers/security' },
      { text: '内容安全与审核', link: '/layers/content-safety' },
      { text: '隐私与数据治理', link: '/layers/privacy' },
      { text: '合规与法律', link: '/layers/compliance' }
    ]
  },
  {
    text: '动手',
    items: [
      { text: '从零实现 Mini Agent', link: '/practice/mini-agent' },
      { text: 'v2：加 RAG、路由与记忆治理', link: '/practice/mini-agent-v2' },
      { text: 'v3：工具网关、计费与持久化', link: '/practice/mini-agent-v3' },
      { text: '部署形态：从能跑到能上线', link: '/practice/deployment' },
      { text: '真实案例拆解', link: '/practice/cases' },
      { text: '交互与信任设计', link: '/practice/ux' }
    ]
  },
  {
    text: '进阶',
    items: [
      { text: '优化手册', link: '/advanced/optimization' },
      { text: '长任务与可靠执行', link: '/advanced/durability' },
      { text: '框架对照与迁移', link: '/advanced/frameworks' },
      { text: '前沿与趋势', link: '/advanced/frontier' }
    ]
  },
  {
    text: '附录',
    items: [
      { text: '术语表', link: '/appendix/glossary' },
      { text: '推荐来源', link: '/appendix/sources' },
      { text: '延伸阅读', link: '/appendix/reading' }
    ]
  }
]
