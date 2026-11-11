<!--
  友链朋友圈 · Hexo 页面模板

  放到 source/fcircle/index.md（或任意页面），改两处：
    1. API_KEY  —— 后台「设置 → API Key」拿到的密钥
    2. BASE_URL —— 后端地址（默认就是线上服务，一般不用改）

  注意：Hexo 是静态站点，页面加载后才向 API 取数据，
  所以这个页面在构建时是空的，浏览器打开才有内容。
-->

{% set API_KEY = 'fc_你的密钥' %}
{% set BASE_URL = 'https://ducircle.dusays.com' %}

<div id="friend-circle">
  <div class="fc-state">加载中…</div>
</div>

<script>
(function () {
  var API_KEY = '{{ API_KEY }}';
  var BASE_URL = '{{ BASE_URL }}';
  var PAGE_SIZE = 10;

  var box = document.getElementById('friend-circle');
  var loaded = 0, total = 0, busy = false;

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function timeAgo(iso) {
    if (!iso) return '';
    var diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
    return new Date(iso).toLocaleDateString('zh-CN');
  }

  function renderItem(p) {
    var avatar = p.avatar
      ? '<img class="fc-avatar" src="' + esc(p.avatar) + '" alt="" loading="lazy" onerror="this.style.visibility=\'hidden\'">'
      : '<div class="fc-avatar"></div>';
    var summary = p.summary ? '<div class="fc-summary">' + esc(p.summary) + '</div>' : '';
    var score = p.score != null ? '<span class="fc-score">' + Number(p.score).toFixed(1) + '</span>' : '';
    return '<li class="fc-item">' + avatar +
      '<div class="fc-body">' +
        '<a class="fc-title" href="' + esc(p.link) + '" target="_blank" rel="noopener noreferrer">' + esc(p.title) + '</a>' +
        '<div class="fc-meta"><span>' + esc(p.author) + '</span><span>·</span>' +
          '<span>' + timeAgo(p.created) + '</span>' + score + '</div>' +
        summary +
      '</div></li>';
  }

  function load(reset) {
    if (busy) return;
    busy = true;
    if (reset) {
      loaded = 0;
      box.innerHTML = '<div class="fc-state">加载中…</div>';
    }
    var url = BASE_URL + '/v1/all?start=' + loaded + '&end=' + (loaded + PAGE_SIZE - 1) + '&rule=created';
    fetch(url, { headers: { 'X-API-Key': API_KEY, 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('请求失败（' + res.status + '）');
        return res.json();
      })
      .then(function (data) {
        total = data.total;
        var html = data.data.length
          ? '<ul class="fc-list">' + data.data.map(renderItem).join('') + '</ul>'
          : '<div class="fc-state">暂时还没有文章</div>';
        if (reset) {
          box.innerHTML = html;
        } else {
          var list = box.querySelector('.fc-list');
          if (list) {
            list.insertAdjacentHTML('beforeend', data.data.map(renderItem).join(''));
          } else {
            box.innerHTML = html;
          }
        }
        loaded += data.data.length;
        var remain = total - loaded;
        var more = box.querySelector('.fc-more');
        if (remain > 0) {
          if (more) {
            more.textContent = '加载更多（还有 ' + remain + ' 篇）';
            more.disabled = false;
          } else {
            var btn = document.createElement('button');
            btn.className = 'fc-more';
            btn.textContent = '加载更多（还有 ' + remain + ' 篇）';
            btn.onclick = function () { load(false); };
            box.appendChild(btn);
          }
        } else if (more) {
          more.remove();
        }
      })
      .catch(function (err) {
        box.innerHTML = '<div class="fc-state">' + esc(err.message) + '</div>';
        console.error('[friend-circle]', err);
      })
      .finally(function () { busy = false; });
  }

  load(true);
})();
</script>

<style>
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
</style>
