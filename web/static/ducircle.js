/**
 * 友链朋友圈 · 前台调用代码
 *
 * 复制即用。改两处：
 *   1. API_BASE —— 后端地址
 *   2. API_KEY  —— 在后台「设置 → API Key」拿到的密钥
 *
 * 用法（原生 JS）：
 *   <div id="friend-circle"></div>
 *   <script src="ducircle.js"></script>
 *   <script>FriendCircle.mount('#friend-circle')</script>
 */

const FriendCircle = (() => {
  // ---- 改这里 ----
  const API_BASE = 'https://ducircle.dusays.com';
  const API_KEY = 'fc_你的密钥';
  // ----------------

  const ENDPOINTS = {
    posts: '/v1/all',
    friends: '/v1/friends',
    stats: '/v1/stats',
  };

  /**
   * 请求只读 API。
   *
   * 鉴权两种都支持：X-API-Key 头（本文用这个）或 Authorization: Bearer。
   * 后者更通用，如果你的构建工具对自定义头有限制，换成它即可。
   */
  async function request(path, params = {}) {
    const url = new URL(API_BASE + path);
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') {
        url.searchParams.set(k, v);
      }
    }

    const res = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        'X-API-Key': API_KEY,
        'Accept': 'application/json',
      },
    });

    if (!res.ok) {
      // 401 是 Key 不对或没带；429 是被限流了（默认 5 QPS）
      let detail = `请求失败（${res.status}）`;
      try {
        const body = await res.json();
        if (body?.detail) detail = body.detail;
      } catch (e) { /* 响应体不是 JSON，用默认文案 */ }
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  /** 文章流。start/end 是「条数偏移」不是页码：start=0&end=9 取前 10 条。 */
  function getPosts({ start = 0, end = 19, rule = 'created', siteId = null, onlySummarized = false } = {}) {
    return request(ENDPOINTS.posts, {
      start,
      end,
      rule,
      site_id: siteId,
      only_summarized: onlySummarized,
    });
  }

  /** 友链列表。 */
  function getFriends() {
    return request(ENDPOINTS.friends);
  }

  /** 统计概览。 */
  function getStats() {
    return request(ENDPOINTS.stats);
  }

  /** ISO 时间转「几天前」。 */
  function timeAgo(iso) {
    if (!iso) return '';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
    return new Date(iso).toLocaleDateString('zh-CN');
  }

  /** 转义，防止标题里的尖括号破坏 HTML。 */
  function esc(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
  }

  const STYLES = `
    .fc-list { list-style: none; margin: 0; padding: 0; }
    .fc-item { display: flex; gap: 12px; padding: 14px 0; border-bottom: 1px solid rgba(0,0,0,.06); }
    .fc-item:last-child { border-bottom: 0; }
    .fc-avatar { width: 36px; height: 36px; border-radius: 10px; flex-shrink: 0;
                 object-fit: cover; background: rgba(0,0,0,.05); }
    .fc-body { flex: 1; min-width: 0; }
    .fc-title { font-size: 15px; font-weight: 600; line-height: 1.5; color: inherit;
                text-decoration: none; display: block;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .fc-title:hover { opacity: .8; }
    .fc-meta { font-size: 12px; color: rgba(0,0,0,.45); margin-top: 4px;
               display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .fc-summary { font-size: 13px; color: rgba(0,0,0,.6); margin-top: 6px; line-height: 1.7;
                  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
                  overflow: hidden; }
    .fc-score { font-size: 11px; padding: 1px 6px; border-radius: 5px;
                background: rgba(230,162,60,.15); color: #a8741c; font-variant-numeric: tabular-nums; }
    .fc-state { padding: 28px 0; text-align: center; font-size: 13px; color: rgba(0,0,0,.45); }
    .fc-more { display: block; width: 100%; margin-top: 14px; padding: 9px;
               border: 1px solid rgba(0,0,0,.12); border-radius: 8px; background: transparent;
               color: inherit; font-size: 13px; cursor: pointer; }
    .fc-more:hover { background: rgba(0,0,0,.03); }
    .fc-more:disabled { opacity: .5; cursor: default; }
  `;

  /**
   * 渲染文章流到指定容器。
   *
   * @param {string} selector  容器选择器
   * @param {object} options   { pageSize, rule, showSummary, showScore, template }
   */
  async function mount(selector, options = {}) {
    const {
      pageSize = 10,
      rule = 'created',
      showSummary = true,
      showScore = true,
      template = null,
    } = options;

    const box = typeof selector === 'string'
      ? document.querySelector(selector)
      : selector;
    if (!box) {
      console.error('[FriendCircle] 找不到容器：', selector);
      return;
    }

    // 样式只注入一次
    if (!document.getElementById('fc-style')) {
      const style = document.createElement('style');
      style.id = 'fc-style';
      style.textContent = STYLES;
      document.head.appendChild(style);
    }

    let loaded = 0;
    let total = 0;
    let busy = false;

    const render = (posts) => {
      if (!posts.length) {
        return '<div class="fc-state">暂时还没有文章</div>';
      }
      return `<ul class="fc-list">${posts.map((p) => {
        if (template) return template(p);
        const avatar = p.avatar
          ? `<img class="fc-avatar" src="${esc(p.avatar)}" alt="" loading="lazy"
                 onerror="this.style.visibility='hidden'">`
          : `<div class="fc-avatar"></div>`;
        const summary = showSummary && p.summary
          ? `<div class="fc-summary">${esc(p.summary)}</div>` : '';
        const score = showScore && p.score != null
          ? `<span class="fc-score">${Number(p.score).toFixed(1)}</span>` : '';
        return `
          <li class="fc-item">
            ${avatar}
            <div class="fc-body">
              <a class="fc-title" href="${esc(p.link)}" target="_blank" rel="noopener noreferrer"
                 title="${esc(p.title)}">${esc(p.title)}</a>
              <div class="fc-meta">
                <span>${esc(p.author)}</span>
                <span>·</span>
                <span>${timeAgo(p.created)}</span>
                ${score}
              </div>
              ${summary}
            </div>
          </li>`;
      }).join('')}</ul>`;
    };

    // 列表和「加载更多」分开两个容器，避免每次重拼整段 HTML
    const listBox = document.createElement('div');
    const moreBox = document.createElement('div');
    box.innerHTML = '';
    box.appendChild(listBox);
    box.appendChild(moreBox);

    const loadMore = async (reset = false) => {
      if (busy) return;
      busy = true;
      if (reset) {
        loaded = 0;
        listBox.innerHTML = '<div class="fc-state">加载中…</div>';
        moreBox.innerHTML = '';
      } else {
        moreBox.innerHTML = '<button class="fc-more" disabled>加载中…</button>';
      }
      try {
        const data = await getPosts({
          start: loaded,
          end: loaded + pageSize - 1,
          rule,
        });
        total = data.total;

        const html = render(data.data);
        listBox.innerHTML = reset ? html : listBox.innerHTML + html;
        loaded += data.data.length;

        const remain = total - loaded;
        moreBox.innerHTML = remain > 0
          ? `<button class="fc-more">加载更多（还有 ${remain} 篇）</button>`
          : '';
        moreBox.querySelector('.fc-more')?.addEventListener('click', () => loadMore(false));
      } catch (err) {
        listBox.innerHTML = `<div class="fc-state">${esc(err.message)}</div>`;
        moreBox.innerHTML = '';
        console.error('[FriendCircle]', err);
      } finally {
        busy = false;
      }
    };

    await loadMore(true);
  }

  return { mount, getPosts, getFriends, getStats, timeAgo };
})();

// 浏览器环境自动挂载
if (typeof window !== 'undefined') {
  window.FriendCircle = FriendCircle;
}
