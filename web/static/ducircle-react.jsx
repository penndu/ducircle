/**
 * 友链朋友圈 · React 组件
 *
 * 用法：
 *   import FriendCircle from './ducircle-react';
 *   <FriendCircle apiKey={process.env.NEXT_PUBLIC_DUCIRCLE_KEY} pageSize={10} />
 *
 * 需要 react >= 18（用了 useEffect / useState）。
 */

import React, { useCallback, useEffect, useState } from 'react';

const API_BASE = process.env.NEXT_PUBLIC_DUCIRCLE_API || 'https://ducircle.dusays.com';

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(iso).toLocaleDateString('zh-CN');
}

export default function FriendCircle({
  apiKey,
  apiBase = API_BASE,
  pageSize = 10,
  rule = 'created',
  showSummary = true,
  showScore = true,
  renderItem = null,
}) {
  const [posts, setPosts] = useState([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(0);
  const [status, setStatus] = useState('loading'); // loading | ok | error
  const [error, setError] = useState('');

  const fetchPosts = useCallback(async (start) => {
    setStatus('loading');
    try {
      const url = new URL(apiBase + '/v1/all');
      url.searchParams.set('start', start);
      url.searchParams.set('end', start + pageSize - 1);
      url.searchParams.set('rule', rule);

      const res = await fetch(url.toString(), {
        headers: {
          'X-API-Key': apiKey,
          Accept: 'application/json',
        },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `请求失败（${res.status}）`);
      }
      const data = await res.json();
      setTotal(data.total);
      setLoaded(start + data.data.length);
      setPosts((prev) => (start === 0 ? data.data : [...prev, ...data.data]));
      setStatus('ok');
    } catch (e) {
      setError(e.message);
      setStatus('error');
    }
  }, [apiKey, apiBase, pageSize, rule]);

  useEffect(() => {
    fetchPosts(0);
  }, [fetchPosts]);

  if (status === 'error') {
    return <div className="fc-state">加载失败：{error}</div>;
  }

  if (status === 'loading' && posts.length === 0) {
    return <div className="fc-state">加载中…</div>;
  }

  return (
    <div className="fc-list-wrap">
      <ul className="fc-list">
        {posts.map((p, i) => (
          <li className="fc-item" key={`${p.link}-${i}`}>
            {p.avatar ? (
              <img
                className="fc-avatar"
                src={p.avatar}
                alt=""
                loading="lazy"
                onError={(e) => { e.currentTarget.style.visibility = 'hidden'; }}
              />
            ) : (
              <div className="fc-avatar" />
            )}
            <div className="fc-body">
              {renderItem ? (
                renderItem(p)
              ) : (
                <>
                  <a className="fc-title" href={p.link} target="_blank" rel="noopener noreferrer">
                    {p.title}
                  </a>
                  <div className="fc-meta">
                    <span>{p.author}</span>
                    <span>·</span>
                    <span>{timeAgo(p.created)}</span>
                    {showScore && p.score != null && (
                      <span className="fc-score">{Number(p.score).toFixed(1)}</span>
                    )}
                  </div>
                  {showSummary && p.summary && (
                    <div className="fc-summary">{p.summary}</div>
                  )}
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      {loaded < total && (
        <button
          className="fc-more"
          disabled={status === 'loading'}
          onClick={() => fetchPosts(loaded)}
        >
          {status === 'loading' ? '加载中…' : `加载更多（还有 ${total - loaded} 篇）`}
        </button>
      )}
    </div>
  );
}
