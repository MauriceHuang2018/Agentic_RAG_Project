// zh-CN locale messages. Keep keys stable; values may be tuned in T6.1.

export default {
  app: {
    title: 'DocGPT · Agentic RAG',
  },
  nav: {
    chat: '智能问答',
    admin: '管理后台',
    profile: '个人中心',
    logout: '退出登录',
  },
  login: {
    username: '用户名',
    password: '密码',
    submit: '登录',
    failed: '登录失败，请检查用户名或密码',
    disabled: '账号已被冻结，请联系管理员',
  },
  chat: {
    placeholder: '请输入你的问题…',
    send: '发送',
    stop: '停止生成',
    feedbackLike: '有帮助',
    feedbackDislike: '没帮助',
    citation: '引用',
    emptyHistory: '暂无历史会话',
    newChat: '新建对话',
    guardrail: {
      sensitive_word: '敏感词命中，已记录审计，请调整后重试',
      prompt_injection: '提问包含越权指令，已记录审计',
      pii: '提问包含个人敏感信息（PII），已脱敏处理或被拒',
      out_of_scope: '问题超出本系统支持的范围（仅限 KB 内检索）',
    },
  },
  feedback: {
    title: '提交反馈',
    scoreUp: '👍 有帮助',
    scoreDown: '👎 没帮助',
    commentPlaceholder: '请告诉我们哪里可以改进…',
    reasonTag: '原因标签',
    category: '类别',
    submit: '提交',
    cancel: '取消',
    commentRequired: '请补充您不满意的点，便于我们改进',
  },
  common: {
    loading: '加载中…',
    empty: '暂无数据',
    confirm: '确认',
    cancel: '取消',
    save: '保存',
    delete: '删除',
    edit: '编辑',
  },
  errors: {
    network: '网络错误，请稍后重试',
    unauthorized: '登录已失效，请重新登录',
    forbidden: '权限不足',
    notFound: '资源不存在',
    internal: '服务器内部错误',
  },
};