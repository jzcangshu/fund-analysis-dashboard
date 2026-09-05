# 项目运行与部署信息

## 生产服务器

- 地址：`8.217.191.193`
- SSH 密钥：使用本机 `C:\Users\jzcan\.ssh\main.pem` 对应的派生公钥。
- `main.pem` 是私钥文件，不得提交、复制到仓库或写入日志；连接时通过 SSH（安全远程登录）客户端的 `-i` 参数引用。

## 当前生产版本

- 部署 commit：`332f5b0 fix: harden production data and operations paths`
- 部署日期：2026-09-06（服务器 `/opt/fund-dashboard` `git rev-parse HEAD` 已核实为 332f5b0）
- 本地发布标签：`prod-20260906-332f5b0`
- 复核方式：`git tag --sort=-creatordate` 查最新 prod 标签；服务器 `/opt/fund-dashboard` 内 `git rev-parse HEAD` 应为本次部署 commit。

> 此次部署包括以下 fix（按提交链）：
> 1. **`4a0764c`**：Caddyfile 移除错误的 reverse_proxy 子指令；api 健康检查 start_period 拉到 60s、retries 8、interval 20s，避免冷启被误杀。
> 2. **`31baf59`**：Caddyfile 给 `/api/*` 加上 transport dial/read/write/response_header_timeout，丢掉 `fail_duration` 避免被动熔断把后续请求挤成 503。
> 3. **`9b7c5fa`**：nav-series 默认窗口限制到 365 天（前 1 年），api mem_limit 从 512m 提到 1g，避免 `select ValuationVersion + FundDailySnapshot` 拉取 2000+ 行被 OOM 杀成 exitCode=137 而重启循环。
> 4. **`a3cd493`**：前端 NavSeries/Positions/Quality tab 切产品时 useEffect 重新拉取，错误细节进 console + Alert，让用户能看到真实 HTTP status 而不是被翻译成统一的「加载失败」。
> 5. **`93796c4`**（本次）：独立代码审计后修复两处残余 OOM 面——导出端点补默认 365 天窗口；nav_series 显式窗口超 5 年返回 422 拒绝；补 end-only / 5y 上限测试；删除测试死 import；修正本文件部署记录为实际生产 HEAD。

## 安全约束

- 不在仓库记录私钥内容、密码、令牌或生产授权码。
- 生产部署前先确认目标主机、SSH 用户和部署目录，再执行数据库迁移、服务重启等有影响操作。
- 每次成功部署后，按 `prod-YYYYMMDD-<短hash>` 形式在本地打一个 tag（不要 push 到 origin），并在本节同步更新"部署 commit / 部署日期"。

## 生产验证防踩坑

以下两条是部署后在服务器上做端到端验证时容易踩的坑，属于部署架构特性而非代码 BUG：

### 1. API 端口不在宿主机暴露——不要 curl 127.0.0.1:8000

`deploy/compose.prod.yml` 中 api 服务用的是 `expose: "8000"`（仅 Docker 网络内部可见），不是 `ports: ["8000:8000"]`（宿主机映射）。只有 caddy 服务映射了 `80:80` / `443:443`。

因此在服务器宿主机上直接 `curl http://127.0.0.1:8000/api/...` 会得到 exit code 7（连接被拒绝）/ HTTP 000。正确的验证路径有两条：

- **经 Caddy 反向代理**：`curl https://127.0.0.1/api/v1/...` 或 `curl https://danyintouzi.com/api/v1/...`（后者走 Cloudflare，与真实用户路径一致）。
- **进容器内部**：`docker exec fund-dashboard-api-1 python -c "import urllib.request; ..."` 或 `docker exec fund-dashboard-api-1 curl http://127.0.0.1:8000/...`。

### 2. 鉴权用 Cookie 不是 Bearer Token——登录不返回 token

登录端点 `POST /api/v1/auth/login` 返回的是用户信息 JSON，并通过 `Set-Cookie: fund_session=<token>` 设置会话，**响应体里没有 `token` 字段**。后续受保护端点只认 `fund_session` Cookie，不认 `Authorization: Bearer` 头。

因此验证受保护接口时必须用 cookie jar 保持会话：

```bash
curl -c /tmp/cookies.txt -X POST https://danyintouzi.com/api/v1/auth/login \
  -H "Content-Type: application/json" -d '{"username":"...","password":"..."}'
curl -b /tmp/cookies.txt https://danyintouzi.com/api/v1/funds/1/nav-series
```

直接从登录响应里取 `data.token` 会得到 `KeyError`，用 `Authorization: Bearer` 访问受保护端点会得到 401 `Not authenticated`。
