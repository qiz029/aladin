# Aladin 公网 404 排查

2026-09-23 UTC。当前状态：已修复，用户原公网浏览器标签页验证通过。

## 证据

- workstation `http://<workstation-ip>:8765/` 返回 200；从 Mac mini 带 `Host: aladin.example.com` 请求也返回 200。
- 公网未登录请求返回 Cloudflare Access 302，登录页返回 200。这只能证明 Access 入口，不能证明登录后的 tunnel 路由。
- 用户已登录的 Chrome 公网页面返回 HTTP 404。
- DNS 已指向共享的 Cloudflare tunnel（名称与 ID 属个人基础设施，不入库）。`cloudflared tunnel route dns` 确認已存在且指向正确 tunnel，没有变更记录。
- Mac mini connector `5bedb0b7-0cdc-4ee2-8299-e9ddebf8de05`，进程 9019，本地 `~/.cloudflared/config.yml` 含正确 Aladin 路由。
- 但进程日志在 `2026-09-22T05:26:33Z` 明确显示远程配置 version 22 覆盖了配置；17 个 hostname 规则均无 Aladin，最后是 `http_status:404`。
- Cloudflare 控制台 Published application routes 同样只有这 17 条，无 Aladin。

## 修复范围

在现有远程配置的 catch-all 前新增唯一规则：

```json
{"hostname":"aladin.example.com","service":"http://<workstation-ip>:8765"}
```

保持其他路由、DNS、Google 身份提供方及 `aladin-private-party` Access 策略。远程配置应热更新，无需重启共享 tunnel。

## 修复前阻碍

控制台添加页面已填入准确域名和源站，但保存因同名 DNS 记录存在被拒绝，未形成配置更新。
通过现有 Mac mini tunnel 凭据调用 Cloudflare API 的方法被自动审批拒绝，原因是需要用户明确授权该凭据来源；已请求用户授权。

## 验收

必须看到 connector 日志的新 configuration version 包含该规则，并在登录后的真实公网浏览器页面加载 Aladin。单独的源站 200、Access 302 或配置文件存在均不足以判定完成。

另观察到本机短暂 DNS 负缓存：系统解析只返回不可路由的 IPv6，后来恢复 A 记录。它并不能解释用户持续的已登录 HTTP 404；勿将两者混为一谈。

## 完成记录

- 用户明确授权使用 Mac mini `~/.cloudflared/cert.pem`，仅调用 Cloudflare API 修复该路由；凭据只在远端内存中用于官方 API 请求，未输出或另存。
- 远程配置 version 22 → 23；仅新增 Aladin ingress，其余 18 条规则（含 catch-all）与其他配置完全保留，API 读回深度比较一致。
- `2026-09-23T06:46:50Z`，原连接器日志确认热加载 version 23，包含 `aladin.example.com → http://<workstation-ip>:8765`；未重启共享 tunnel。
- 刷新用户原来返回 HTTP 404 的 Chrome 标签页，`https://aladin.example.com/` 正常显示 `工作室 · aladin`、主导航和作品列表。
- 未改动 DNS、Google 身份提供方或 Access 白名单。
- 修复前后远程配置快照：`.cache/aladin-route-repair/config-update.json`（不含认证凭据）。
