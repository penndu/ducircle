/**
 * 友链朋友圈 · Vue 3 组件
 *
 * 用法：
 *   import FriendCircle from './ducircle-vue.js';
 *   <FriendCircle :api-key="key" :page-size="10" />
 */

import { ref, onMounted, watch } from 'vue';

const API_BASE = 'https://ducircle.dusays.com';

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(iso).toLocaleDateString('zh-CN');
}

export default {
  name: 'FriendCircle',
  props: {
    apiKey: { type: String, required: true },
    apiBase: { type: String, default: API_BASE },
    pageSize: { type: Number, default: 10 },
    rule: { type: String, default: 'created' },
    showSummary: { type: Boolean, default: true },
    showScore: { type: Boolean, default: true },
  },
  setup(props) {
    const posts = ref([]);
    const total = ref(0);
    const loaded = ref(0);
    const status = ref('loading');
    const error = ref('');

    async function fetchPosts(start) {
      status.value = 'loading';
      error.value = '';
      try {
        const url = new URL(props.apiBase + '/v1/all');
        url.searchParams.set('start', start);
        url.searchParams.set('end', start + props.pageSize - 1);
        url.searchParams.set('rule', props.rule);

        const res = await fetch(url.toString(), {
          headers: {
            'X-API-Key': props.apiKey,
            Accept: 'application/json',
          },
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `请求失败（${res.status}）`);
        }
        const data = await res.json();
        total.value = data.total;
        loaded.value = start + data.data.length;
        posts.value = start === 0 ? data.data : [...posts.value, ...data.data];
        status.value = 'ok';
      } catch (e) {
        error.value = e.message;
        status.value = 'error';
      }
    }

    onMounted(() => fetchPosts(0));
    watch(() => [props.apiKey, props.rule, props.pageSize], () => fetchPosts(0));

    return {
      posts, total, loaded, status, error, fetchPosts, timeAgo,
    };
  },
  template: `
    <div class="fc-list-wrap">
      <div v-if="status === 'error'" class="fc-state">加载失败：{{ error }}</div>
      <div v-else-if="status === 'loading' && !posts.length" class="fc-state">加载中…</div>
      <ul v-else class="fc-list">
        <li v-for="(p, i) in posts" :key="p.link + i" class="fc-item">
          <img v-if="p.avatar" class="fc-avatar" :src="p.avatar" alt="" loading="lazy"
               @error="$event.target.style.visibility = 'hidden'" />
          <div v-else class="fc-avatar"></div>
          <div class="fc-body">
            <a class="fc-title" :href="p.link" target="_blank" rel="noopener noreferrer">{{ p.title }}</a>
            <div class="fc-meta">
              <span>{{ p.author }}</span>
              <span>·</span>
              <span>{{ timeAgo(p.created) }}</span>
              <span v-if="showScore && p.score != null" class="fc-score">{{ Number(p.score).toFixed(1) }}</span>
            </div>
            <div v-if="showSummary && p.summary" class="fc-summary">{{ p.summary }}</div>
          </div>
        </li>
      </ul>
      <button v-if="loaded < total" class="fc-more" :disabled="status === 'loading'" @click="fetchPosts(loaded)">
        {{ status === 'loading' ? '加载中…' : '加载更多（还有 ' + (total - loaded) + ' 篇）' }}
      </button>
    </div>
  `,
};
