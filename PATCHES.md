# 本 fork 的本地修改（local patches）

这是**自用部署**的产物，不是准备提给上游的 PR。修改全部落在分支 **`local-patches`**，
基点是上游 tag **0.6.27**（提交 `c3756a903e7d`）。

- 上游项目：<https://github.com/janeczku/calibre-web>（GPL-3.0-or-later）
- 本分支的修改同样按 GPL-3.0-or-later 分发，`LICENSE` 沿用上游原文
- 与上游、linuxserver.io、Calibre、豆瓣均无关联，也未获其背书
- 这些改动**只改动 5 个文件**，每个改动一个提交，便于 rebase 或单独摘除

## 改了什么

| 提交 | 文件 | 内容 |
|---|---|---|
| 本地修改说明 | `PATCHES.md` | 本文件 |
| 封面下载补 Referer | `cps/helper.py` | 新增 `_cover_referer_headers(url)`：按主机分流 Referer（`*.douban.com`/`*.doubanio.com` → `https://book.douban.com/`，其它站点 → 同源 `scheme://host/`），并给 `save_cover_from_url()` 的两条下载分支（`cw_advocate.get` 与 `requests.get`）都带上 |
| 元数据预览封面改服务端代理 | `cps/search_metadata.py`、`cps/templates/book_edit.html`、`cps/static/js/get_meta.js` | 新增同源路由 `/metadata/cover?url=...`（校验协议、限定 `image/*`、限 10MB、`Cache-Control: public, max-age=86400`）；搜索结果新增 `cover_preview` 字段供显示，`cover` **保持绝对 URL 不变** |
| 保存封面兜底 | `cps/helper.py` | `save_cover_from_url()` 收到站内代理相对路径时拆出内层绝对 URL 继续下载；并把 `requests.exceptions.InvalidSchema` / `MissingSchema` 加进 except 名单 |
| 移动端侧栏位移修复 | `cps/static/css/style.css` | 追加 `@media (max-width: 509px)` 规则，让侧栏导航在移动端从一开始就不渲染 |

## 为什么

1. **「获取元数据 → 下载封面」必然失败**（日志 `Cover Download Error 418`）：豆瓣图床
   （`img*.doubanio.com`）有防盗链 —— 无 Referer → **418**，自建站点域名 Referer → **403**。
   上游两条下载分支都不带 Referer 头，因此必然被拒。

2. **元数据搜索结果的封面在浏览器里加载不出来**：页面直连图片站会被防盗链 + CSP
   `img-src 'self' data:` 双重拦下，而浏览器侧无法伪造 Referer。改为由服务端代取。

3. **「保存元数据」整个请求 500**：第 2 项最初的做法是把 `record["cover"]` 就地改写成
   站内相对路径，而该字段同时被 `get_meta.js` 当作表单「Fetch Cover from URL」的值提交，
   于是 `helper.save_cover_from_url()` 收到相对路径抛 `requests.InvalidSchema`，
   且它不在 except 名单里 → 保存请求 500。改为新增 `cover_preview` 字段（显示用），
   `cover` 保持绝对 URL；再加一道兜底把内层 URL 拆出来。

4. **手机端首屏 89px 布局位移**（CLS 0.085）：`intention.js` 在视口宽度 `<510px` 时把侧栏
   `#scnd-nav` 搬进汉堡菜单，侧栏列随之塌陷（90px → 1px）；首帧已经画过，约 250ms
   后才塌，于是内容整体上移。补一段同一断点的 CSS 让它一开始就不渲染 —— 与搬走后的
   终态一致。**断点必须是 509px**：上游 `cps/static/js/libs/context.min.js` 里
   `standard ≥840 / tablet ≥510 / mobile <510`，写成 767/768 会让 510–839px 区间
   （导航本应留在侧栏）的菜单消失。

实测（真实浏览器）：封面下载 418 → 200；手机端 CLS **0.0846 → 0.0004**，
390 / 600 / 1200px 三档宽度下导航都能点开。

## 部署

本分支源码可直接构建运行。若只想"叠在现成镜像上"而不想自己维护源码树，
等价且可复现的构建方式（Dockerfile + 同一批补丁脚本 + 云端 CI 断言）见：
**<https://github.com/Emopeny/calibre-web-patches>**

## 注意

- 这些改动在「中文书库 + 豆瓣元数据源 + calibre-web 0.6.27」场景下验证；
  豆瓣侧没有公开 API，其防盗链策略随时可能变化。
- 上游升级后 rebase 本分支时，`cps/helper.py` 的 except 名单与
  `cps/static/css/style.css` 的追加块最可能冲突。
