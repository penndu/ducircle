# 构建与部署

服务器内存只有 1.7G，在上面 `docker build` 容易 OOM。
所以构建放在 GitHub Actions，产物推到 Docker Hub，服务器只负责拉取。

**构建自动，部署手动。** 这样做的原因：自动部署意味着 SSH 私钥
要放进 GitHub Secrets —— 一旦泄露，攻击者能以 root 登进服务器。
而这台机器上还跑着 artalk、memos、phpmyadmin 等服务，牵连面太大。
手动部署用本机密钥，风险可控得多。

顺带得到一个保证：**构建不过就没有镜像可拉**，坏版本上不了线。

---

## 一次性配置

### 1. Docker Hub 准备

**创建仓库**：<https://hub.docker.com/repositories/> → Create repository
→ 名字填 `ducircle`，可见性选 Public

**创建 Access Token**（不是登录密码）：

1. 打开 <https://hub.docker.com/settings/access-tokens>
2. Account settings → Personal access tokens → Generate new token
3. 勾选 `Read & Write`（或 `Read only`，见下方说明）
4. 生成后**立刻复制**——只显示一次

> **建议勾 Read & Write 还是 Read Only？**
> 构建推镜像需要 Write；服务器 pull 是匿名公开拉取，不需要 Token。
> 如果你打算只让 CI 推、服务器匿名拉，用 Read & Write 即可。
> 最小权限原则可用 Write，但不要勾其他无关权限。

### 2. 配置 Actions Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| 名称 | 值 |
|---|---|
| `DOCKERHUB_USERNAME` | `penndu` |
| `DOCKERHUB_TOKEN` | 上一步复制的 Access Token |

**就这两个，不需要任何服务器相关的密钥。**

这是相比自动部署最大的好处：GitHub 里不存私钥。
部署用本机的 `ssh dusays`，不走 GitHub。

---

### 3. 服务器上的 `.env`

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index

# 首次：克隆代码
git clone git@github.com:penndu/ducircle.git .

# 生成 .env
cp .env.example .env
echo "" >> .env
echo "SECRET_KEY=$(openssl rand -base64 48)" >> .env
chmod 600 .env
```

填入数据库与 Redis 密码（见部署清单第三节）。

### 4. 容器接入网络

**这一步 1Panel 用户最容易漏。** 在 1Panel「容器」页面，
给应用容器加入 MySQL 和 Redis 所在的网络：

- `1Panel-mysql-wQr4` 所属网络
- `1Panel-redis-29uO` 所属网络

验证：

```bash
docker exec ducircle getent hosts 1Panel-mysql-wQr4
```

---

## 首次部署

### 1. 触发构建

```bash
git commit --allow-empty -m "触发首次构建"
git push
```

看进度：<https://github.com/penndu/ducircle/actions>

### 2. 手动部署（等 Actions 变绿后）

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index

git pull origin main
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
docker logs -f ducircle
```

### 3. 确认版本

```bash
curl -s https://ducircle.dusays.com/health
```

`build` 字段应该是刚才那个时间戳标签。

---

## 日常更新

**三步：push → 看版本号 → SSH 部署。**

```bash
# 1. 本机
git commit -am "改动说明"
git push
```

等 Actions 变绿后，**去 commit 的评论里看版本号**：

<https://github.com/penndu/ducircle/commits/main> → 点最新 commit → 看「镜像构建」评论

```bash
# 2. 服务器（把版本号换成实际的）
ssh dusays 'cd /opt/1panel/www/sites/ducircle.dusays.com/index && \
  git pull origin main && \
  IMAGE_TAG=202610240047 docker compose -f deploy/docker-compose.yml pull && \
  IMAGE_TAG=202610240047 docker compose -f deploy/docker-compose.yml up -d'

# 3. 验证
curl -s https://ducircle.dusays.com/health
# {"status":"ok","db":"ok","version":"0.1.0","build":"202610240047",...}
```

`build` 字段应该等于你部署的版本号。

---

## ⚠️ 必须用时间戳标签部署，不要用 latest

**这是个真实踩过的坑。** Docker Hub 的 manifest 有 CDN 缓存，
短时间内 `docker pull latest` 可能拿到的还是旧的 manifest——
`docker images` 显示的 digest 和上次完全一样，容器跑的也是旧代码，
但 CI 明明显示 push 成功。

用时间戳标签就完全没这个问题：新标签没有缓存。

```bash
# ✅ 对
IMAGE_TAG=202610240047 docker compose -f deploy/docker-compose.yml up -d

# ❌ 错（可能拉到旧镜像）
docker compose -f deploy/docker-compose.yml up -d
```

**怎么确认拉到的对不对：**

```bash
docker exec ducircle printenv DUCIRCLE_VERSION
# 应该输出你指定的版本号
```

### 顺带说下 compose 的 pull_policy

已设 `pull_policy: always`。Docker 默认的 `missing` 策略是
「本地没这个镜像才拉」——本地有了 latest 就永远不更新，
`force-recreate` 也拉不到新的。

但 **pull_policy 也救不了 latest 的 CDN 缓存问题**，
所以还是用时间戳标签。

### 构建失败时

Actions 会失败并把诊断信息显示在运行页面的「输出诊断信息」里：

| 现象 | 原因 |
|---|---|
| `No module named pymysql` | 依赖缺失（加新依赖时注意） |
| `Readme file does not exist` | README.md 没被 Dockerfile COPY（`scripts/check_dockerignore.py` 会在提交前抓到）|
| `Invalid workflow file` | workflow YAML 结构错（`scripts/check_workflow.py` 会在提交前抓到）|
| `buildLog` 里 metadata-generation-failed | 看 build.log 里的 OSError 行 |

服务器侧部署失败时：

| 现象 | 原因 |
|---|---|
| 迁移失败，容器起不来 | 数据库连接问题，检查 `.env` 与容器网络 |
| 健康检查失败 | 应用起来了但 /health 返回非 200，看日志里的数据库状态 |
| 版本号不符 | 拉到了旧镜像，确认 IMAGE_TAG 写对了 |
| `no space left on device` | 磁盘满了，`docker system prune -a` 清理 |

---

## 为什么构建必须在 CI 上

服务器**连不上 Debian软件源**（`Unable to connect to deb.debian.org`），
实测在服务器上 `docker build` 会在装 gcc/curl 那步直接失败。
加上内存只有 1.7G，构建放在 CI 上是唯一可行的方案。

## 镜像标签

每次构建会推三个标签：

| 标签 | 示例 | 用途 |
|---|---|---|
| 时间戳 | `202610221200` | 部署用，精确知道是哪个版本 |
| `latest` | `latest` | 手动 `docker pull` 时的默认 |
| commit sha | `f32f724` | 精确追溯某次提交 |

时间戳用东八区（`TZ=Asia/Shanghai`），不是 UTC —— 否则
日志里的时间会比本地早 8 小时，看新旧版本时容易误判。

---

## 版本验证

部署完先看这个：

```bash
curl -s https://ducircle.dusays.com/health
```

```json
{
  "status": "ok",
  "db": "ok",
  "version": "0.1.0",
  "build": "202610221200",
  "built_at": "2026-10-22T12:00:00+08:00"
}
```

`build` 字段就是当前的镜像版本。部署脚本会自动比对
「实际运行版本」与「期望版本」，不一致直接报错退出。

服务器上还存了一份：

```bash
ssh dusays 'cat /opt/1panel/www/sites/ducircle.dusays.com/index/.deployed-version'
```

---

## 回滚

改一个版本号就行，不用改文件：

```bash
ssh dusays
cd /opt/1panel/www/sites/ducircle.dusays.com/index

IMAGE_TAG=202610221150 docker compose -f deploy/docker-compose.yml pull
IMAGE_TAG=202610221150 docker compose -f deploy/docker-compose.yml up -d
```

`IMAGE_TAG` 不传时默认用 `latest`，所以平时不用管它。

---

## CI 里跑了什么

推送镜像前会执行：

1. **await 位置检查** —— 防止 `await db.execute(q).scalar_one()` 这类错误
   （`.scalar_one()` 会作用在协程上，报错信息完全指不到真因）
2. **MySQL 方言兼容性检查** —— 索引长度、外键、租户表索引覆盖
3. **单元测试** —— 277 个用例
4. **构建镜像** —— 目前只构建 linux/amd64（服务器是 x86）。

   双架构要装 QEMU 模拟，编译 asyncmy 的 C 扩展时极慢且容易超时，
   实际跑过一次失败了。换 arm64 服务器时改 workflow 里的
   `platforms: linux/amd64,linux/arm64` 并加 setup-qemu-buildx-action。

测试不过，镜像不推，服务器不更新。