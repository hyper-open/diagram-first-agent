#!/usr/bin/env node
/**
 * Build a file:// friendly static copy of the tutorial.
 *
 * - plain HTML, relative links, no VitePress runtime
 * - Mermaid is rendered by a local classic script (works from file://)
 * - output: offline/index.html + one HTML per chapter
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import MarkdownIt from 'markdown-it';
import container from 'markdown-it-container';
import { sidebar } from '../docs/.vitepress/sidebar.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const docsDir = path.join(root, 'docs');
const outDir = path.join(root, 'offline');
const offlineAssets = path.join(root, 'tools', 'offline');

function fileForLink(link) {
  if (!link || link === '/') return 'index.html';
  let clean = link.replace(/^\/+/, '').replace(/\.md$/, '');
  if (clean.endsWith('/')) clean += 'index';
  if (!clean.endsWith('.html')) clean += '.html';
  return clean;
}

function relative(fromFile, target) {
  const rel = path.posix.relative(path.posix.dirname(fromFile), target);
  return rel || 'index.html';
}

function rewriteHref(href, fromFile) {
  if (!href) return href;
  if (/^(https?:|mailto:|tel:|#|\/\/)/.test(href)) return href;
  const hashAt = href.indexOf('#');
  const hash = hashAt >= 0 ? href.slice(hashAt) : '';
  let raw = hashAt >= 0 ? href.slice(0, hashAt) : href;
  if (!raw) return href;

  let target;
  if (raw.startsWith('/')) {
    target = raw.replace(/^\/+/, '');
  } else {
    target = path.posix.normalize(
      path.posix.join(path.posix.dirname(fromFile), raw)
    );
  }

  const ext = path.posix.extname(target).toLowerCase();
  if (ext && ext !== '.md' && ext !== '.html') {
    // public assets are copied to offline/assets
    target = 'assets/' + target.replace(/^assets\//, '');
  } else {
    target = target.replace(/\.md$/, '');
    if (target.endsWith('/') || target === '') target += 'index';
    if (!target.endsWith('.html')) target += '.html';
  }
  return relative(fromFile, target) + hash;
}

function rewriteRawLinks(html, fromFile) {
  return html.replace(/\b(href|src)="(\/(?!\/)[^"]*)"/g, (match, attr, value) => {
    return `${attr}="${rewriteHref(value, fromFile)}"`;
  });
}

function stripFrontmatter(source) {
  const match = /^---\s*\n([\s\S]*?)\n---\s*\n/.exec(source);
  if (!match) return { meta: {}, body: source };
  const meta = {};
  for (const line of match[1].split('\n')) {
    const item = /^(\w+):\s*(.*)$/.exec(line.trim());
    if (item) {
      meta[item[1]] = item[2].replace(/^["']|["']$/g, '');
    }
  }
  return { meta, body: source.slice(match[0].length) };
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function slugify(text) {
  return String(text)
    .toLowerCase()
    .replace(/<[^>]+>/g, '')
    .replace(/[\s]+/g, '-')
    .replace(/[!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~，。！？：；、“”‘’（）【】《》…—]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

function extractH1(html) {
  const match = /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(html);
  return match ? match[1].replace(/<[^>]+>/g, '').trim() : '';
}

function renderMarkdown(source, pageFile) {
  const env = { page: pageFile, slugCounts: new Map() };
  const html = md.render(source, env);
  return rewriteRawLinks(html, pageFile);
}

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: false,
  breaks: false
});

md.use(container, 'tip', {
  render(tokens, idx) {
    if (tokens[idx].nesting === 1) {
      const title = tokens[idx].info.trim().slice(3).trim();
      return `<div class="custom-block tip"><p class="custom-block-title">${escapeHtml(title || '提示')}</p>\n`;
    }
    return '</div>\n';
  }
});
md.use(container, 'warning', {
  render(tokens, idx) {
    if (tokens[idx].nesting === 1) {
      const title = tokens[idx].info.trim().slice(7).trim();
      return `<div class="custom-block warning"><p class="custom-block-title">${escapeHtml(title || '注意')}</p>\n`;
    }
    return '</div>\n';
  }
});
md.use(container, 'danger', {
  render(tokens, idx) {
    if (tokens[idx].nesting === 1) {
      const title = tokens[idx].info.trim().slice(6).trim();
      return `<div class="custom-block danger"><p class="custom-block-title">${escapeHtml(title || '警告')}</p>\n`;
    }
    return '</div>\n';
  }
});
md.use(container, 'details', {
  render(tokens, idx) {
    if (tokens[idx].nesting === 1) {
      const title = tokens[idx].info.trim().slice(7).trim();
      return `<details class="custom-block details"><summary>${escapeHtml(title || '展开')}</summary>\n`;
    }
    return '</details>\n';
  }
});

md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx];
  const info = token.info.trim();
  if (info === 'mermaid') {
    return `<div class="mermaid">${escapeHtml(token.content)}</div>\n`;
  }
  const lang = info.split(/\s+/)[0] || 'text';
  return `<div class="code-block"><div class="code-lang">${escapeHtml(lang)}</div><pre><code>${escapeHtml(token.content)}</code></pre></div>\n`;
};

md.renderer.rules.table_open = () => '<div class="table-wrap"><table>\n';
md.renderer.rules.table_close = () => '</table></div>\n';

md.renderer.rules.heading_open = (tokens, idx, options, env, self) => {
  const inline = tokens[idx + 1];
  const text = inline ? inline.content : '';
  const base = slugify(text) || 'section';
  const count = env.slugCounts.get(base) || 0;
  env.slugCounts.set(base, count + 1);
  const id = count ? `${base}-${count}` : base;
  tokens[idx].attrSet('id', id);
  return self.renderToken(tokens, idx, options);
};

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const href = tokens[idx].attrGet('href') || '';
  const rewritten = rewriteHref(href, env.page);
  tokens[idx].attrSet('href', rewritten);
  if (/^https?:/i.test(href)) {
    tokens[idx].attrSet('target', '_blank');
    tokens[idx].attrSet('rel', 'noopener');
  }
  return self.renderToken(tokens, idx, options);
};

md.renderer.rules.image = (tokens, idx, options, env, self) => {
  const src = tokens[idx].attrGet('src') || '';
  tokens[idx].attrSet('src', rewriteHref(src, env.page));
  tokens[idx].attrSet('loading', 'lazy');
  return self.renderToken(tokens, idx, options);
};

const pages = sidebar.flatMap((group) =>
  group.items.map((item) => ({
    text: item.text,
    link: item.link,
    group: group.text,
    file: fileForLink(item.link)
  }))
);

const pageIndex = new Map(pages.map((page, index) => [page.file, index]));

function navHtml(currentFile) {
  return sidebar
    .map((group) => {
      const items = group.items
        .map((item) => {
          const file = fileForLink(item.link);
          const active = file === currentFile ? ' active' : '';
          return `<a class="nav-item${active}" href="${relative(currentFile, file)}">${escapeHtml(item.text)}</a>`;
        })
        .join('\n');
      return `<div class="nav-group"><div class="nav-title">${escapeHtml(group.text)}</div>${items}</div>`;
    })
    .join('\n');
}

function pagerHtml(currentFile) {
  const index = pageIndex.get(currentFile);
  if (index === undefined) return '';
  const prev = pages[index - 1];
  const next = pages[index + 1];
  const prevHtml = prev
    ? `<a class="prev" href="${relative(currentFile, prev.file)}"><span>上一篇</span><strong>${escapeHtml(prev.text)}</strong></a>`
    : '<span class="empty"></span>';
  const nextHtml = next
    ? `<a class="next" href="${relative(currentFile, next.file)}"><span>下一篇</span><strong>${escapeHtml(next.text)}</strong></a>`
    : '<span class="empty"></span>';
  return `<nav class="pager">${prevHtml}${nextHtml}</nav>`;
}

function breadcrumbHtml(page) {
  return `<div class="breadcrumb"><a href="${relative(page.file, 'index.html')}">首页</a><span>/</span><span>${escapeHtml(page.group)}</span><span>/</span><strong>${escapeHtml(page.text)}</strong></div>`;
}

function layout({ title, file, body, breadcrumb = '', pager = '', isHome = false }) {
  const css = relative(file, 'assets/offline.css');
  const js = relative(file, 'assets/offline.js');
  const mermaid = relative(file, 'assets/mermaid.min.js');
  const logo = relative(file, 'assets/logo.svg');
  const home = relative(file, 'index.html');
  const nav = [
    ['首页', 'index.html'],
    ['架构主线', 'evolution/overview.html'],
    ['分层深入', 'layers/model.html'],
    ['实战', 'practice/mini-agent.html'],
    ['优化', 'advanced/optimization.html']
  ]
    .map(([label, target]) => `<a href="${relative(file, target)}">${label}</a>`)
    .join('');
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(title)} · Agent 学习手册（离线版）</title>
  <meta name="description" content="Agent 架构演进教程离线版：从 ReAct 到 Agent 平台。">
  <link rel="icon" href="${logo}">
  <link rel="stylesheet" href="${css}">
</head>
<body class="${isHome ? 'home' : ''}">
  <header class="topbar">
    <button class="menu-btn" id="menuBtn" aria-label="打开菜单">☰</button>
    <a class="brand" href="${home}"><img src="${logo}" alt=""><span>Agent 学习手册</span><em>离线版</em></a>
    <nav class="topnav">${nav}</nav>
  </header>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <input class="nav-filter" id="navFilter" type="search" placeholder="筛选章节…" aria-label="筛选章节">
      ${navHtml(file)}
      <p class="sidebar-note">本地离线版 · 双击 HTML 即可阅读<br>无需启动服务器</p>
    </aside>
    <main class="content">
      <div class="content-inner">
        ${breadcrumb}
        <article class="doc">${body}</article>
        ${pager}
        <footer class="page-footer">Agent 学习手册 · 离线版 · 以架构图为主线的 Agent 教程</footer>
      </div>
    </main>
  </div>
  <div class="overlay" id="overlay"></div>
  <a class="to-top" id="toTop" href="#" hidden>↑</a>
  <noscript><div class="custom-block warning"><p class="custom-block-title">需要启用 JavaScript</p><p>Mermaid 架构图依赖本地 JavaScript 渲染，请在浏览器中允许此文件运行脚本。</p></div></noscript>
  <script src="${mermaid}"></script>
  <script src="${js}"></script>
</body>
</html>
`;
}

function readPage(link) {
  const sourcePath = path.join(docsDir, link.replace(/^\/+/, '') + '.md');
  const raw = fs.readFileSync(sourcePath, 'utf8');
  return stripFrontmatter(raw);
}

function build() {
  fs.rmSync(outDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(outDir, 'assets'), { recursive: true });

  fs.copyFileSync(path.join(root, 'node_modules', 'mermaid', 'dist', 'mermaid.min.js'), path.join(outDir, 'assets', 'mermaid.min.js'));
  fs.copyFileSync(path.join(offlineAssets, 'offline.css'), path.join(outDir, 'assets', 'offline.css'));
  fs.copyFileSync(path.join(offlineAssets, 'offline.js'), path.join(outDir, 'assets', 'offline.js'));

  const publicDir = path.join(docsDir, 'public');
  if (fs.existsSync(publicDir)) {
    for (const entry of fs.readdirSync(publicDir, { withFileTypes: true })) {
      if (entry.isFile()) {
        fs.copyFileSync(path.join(publicDir, entry.name), path.join(outDir, 'assets', entry.name));
      }
    }
  }

  for (const page of pages) {
    const { meta, body } = readPage(page.link);
    const html = renderMarkdown(body, page.file);
    const title = meta.title || extractH1(html) || page.text;
    const outputPath = path.join(outDir, page.file);
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(
      outputPath,
      layout({
        title,
        file: page.file,
        body: html,
        breadcrumb: breadcrumbHtml(page),
        pager: pagerHtml(page.file)
      }),
      'utf8'
    );
  }

  const homeSource = stripFrontmatter(fs.readFileSync(path.join(docsDir, 'index.md'), 'utf8'));
  const homeBody = renderMarkdown(homeSource.body, 'index.html');
  const hero = `<section class="home-hero">
  <div>
    <p class="eyebrow">Agent 学习手册 · 离线版</p>
    <h1>从 ReAct 到平台</h1>
    <p class="tagline">先建立架构分层总览，再沿八级演进逐层加组件。每篇先给主架构图，再用读图步骤、时序图和问题矩阵展开。</p>
    <div class="home-actions">
      <a class="btn btn-primary" href="evolution/overview.html">从架构总览开始</a>
      <a class="btn btn-ghost" href="evolution/react.html">直接看 ReAct</a>
    </div>
  </div>
  <img class="home-logo" src="assets/logo.svg" alt="Agent 学习手册">
</section>`;
  fs.writeFileSync(
    path.join(outDir, 'index.html'),
    layout({
      title: 'Agent 学习手册',
      file: 'index.html',
      body: hero + homeBody,
      isHome: true
    }),
    'utf8'
  );

  fs.writeFileSync(
    path.join(outDir, '如何打开.txt'),
    [
      'Agent 学习手册（diagram-first-agent） · 离线版',
      '',
      '直接双击本目录下的 index.html，即可在浏览器中阅读。',
      '不需要启动服务器，也不需要联网。',
      '',
      '章节之间使用相对路径跳转；左侧目录、上一篇/下一篇、筛选框都可以直接使用。',
      'Mermaid 架构图由本地 assets/mermaid.min.js 渲染，首次打开可能稍慢。',
      '',
      '重新生成：pnpm build:offline',
      ''
    ].join('\n'),
    'utf8'
  );

  console.log(`Offline site built: ${path.join(outDir, 'index.html')}`);
  console.log(`Pages: ${pages.length + 1}`);
}

build();
