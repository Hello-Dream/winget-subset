# winget-subset

**给自己的 Windows 软件管家建一个国内加速版 WinGet 源。**

[WinGet](https://learn.microsoft.com/windows/package-manager/winget/) 是 Windows 官方包管理器，
但它的社区仓库托管在 GitHub 上 —— 国内直连下载安装器经常龟速甚至失败。

本项目让你：

1. 从微软官方全量仓库（40 万+ 包）中**只挑自己用得上的几十个**；
2. 其中 GitHub 托管的安装器**自动改写为你自己的 Cloudflare 加速地址**；
3. 全部打包成一个标准的 WinGet 第三方源，发布到 Cloudflare Pages；
4. 之后 `winget install` / `winget upgrade --all` 与官方体验完全一致，且每日自动跟随上游更新。

```
microsoft/winget-pkgs (微软官方全量仓库)
        │   git 部分克隆（仅拉取你选的包，不碰其余 40 万文件）
        ▼
packages.txt ──► scripts/sync_manifests.py ──► manifests/    子集清单，GitHub 安装器地址已加加速前缀*
                                                       │
                                    scripts/build_index.py + build_source.ps1
                                                       ▼
                                            dist/source.msix    签名后的源索引包
                                                       ▼
                                  Cloudflare Pages（source.msix + manifests/ 静态托管）
                                                       ▼
                     你的电脑: winget source add --name subset https://winget.你的域名.com/
```

\* 只有 `github.com` / `*.githubusercontent.com` 等域名会被加前缀；
钉钉、QQ、微信这类安装器本来就在国内 CDN 上，原样直连不做任何改动。

---

## 一、工作原理（3 分钟读懂）

- **WinGet 第三方源的机制**：客户端从源地址拉取一个 `source.msix` 索引包（内含 SQLite 数据库）用于搜索，
  安装时再按路径去同域名的 `manifests/` 目录读取清单 YAML、下载真正的安装器。
  所以我们要同时发布这两样东西。
- **为什么要签名证书**：WinGet 对第三方源强制做 Authenticode 校验（见
  [winget-cli 的 `ValidateMsixTrustInfo`](https://github.com/microsoft/winget-cli/blob/master/src/AppInstallerCommonCore/MsixInfo.cpp)）。
  我们用一张自签代码签名证书签包，再把公钥导入自己电脑的信任库即可，不需要花钱买证书。
- **改了下载地址，安全吗**：本项目只改清单里 `InstallerUrl` 的主机前缀，安装器的
  `InstallerSha256` 保持微软仓库原值 —— 代理若篡改文件字节，哈希校验必然失败并拒绝安装。
  链路上任何一方都无法在不被发现的情况下注入恶意内容。

## 二、你需要准备

| 项目 | 说明 | 费用 |
|---|---|---|
| GitHub 账号 | 存放仓库、跑自动化 | 公共仓库 Actions 完全免费；私有仓库有免费额度但 Windows runner 计两倍时长，建议用公共仓库 |
| Cloudflare 账号 | 部署 Worker（加速）+ Pages（托管源） | 免费额度足够个人使用 |
| 一台 Windows 电脑 | 生成签名证书、日常 winget 使用 | — |
| Node.js 18+（可选） | 仅 wrangler 命令行部署 Worker 用；不用它可走网页控制台 | — |

全程人工操作约 **20 分钟**，之后完全免维护。

---

## 三、分步教程

### Step 0 · 获取代码并启用 Actions

Fork 本仓库，或下载后推送到你自己的 GitHub 新仓库（公共仓库 Actions 才免费）。

> Fork 后记得进入仓库的 **Actions** 标签页，点击按钮启用工作流；
> 定时任务只在你仓库的默认分支上生效。

### Step 1 · 部署加速 Worker 并绑定自有域名

> ⚠️ **为什么必须用自己的域名**：Cloudflare 的默认域名 `*.workers.dev` 和 `*.pages.dev`
> 在国内均被 DNS 污染，基本无法访问。把域名以 NS 方式托管到你的 Cloudflare 账号后
> （添加站点按提示去注册商改 NS 即可），Worker 和 Pages 都能一键绑定子域名，
> 走 Cloudflare 正常 anycast 网络并自动签发证书，国内一般可达。
> 没有现成域名的话，任意一个便宜域名都够用。

任选一种方式部署：

**方式 A · 自动部署脚本（推荐）**

```bash
# 1. 登录 Cloudflare（首次需要）
npx wrangler login

# 2. 运行部署脚本，自动绑定域名并更新 GitHub Secret
python scripts/deploy_worker.py --domain gh.你的域名.com --repo 你的用户名/仓库名
```

脚本会自动完成：
- 更新 `worker/wrangler.toml` 的 routes 配置
- 执行 `wrangler deploy` 部署 Worker
- 通过 `gh secret set` 更新 GitHub Secret `WINGET_PROXY_PREFIX`

> ⚠️ 需要安装 [GitHub CLI](https://cli.github.com/) 并登录（`gh auth login`）。

**方式 B · 网页控制台（无需任何工具）**

1. 登录 [dash.cloudflare.com](https://dash.cloudflare.com) → 左侧 **Workers & Pages** → **Create application** → **Create Worker**；
2. 名字随意（例如 `gh-proxy`），点 Deploy；
3. 点 **Edit code**，删除示例代码，把 [`worker/src/index.js`](worker/src/index.js) 全文粘贴进去，再次 Deploy；
4. 该 Worker 详情页 → **Settings** → **Domains & Routes** → **Add** → **Custom domain**，
   填一个子域名（例如 `gh.你的域名.com`），确认后 DNS 与证书自动配好。

**方式 C · 命令行手动部署**

```bash
cd worker
npx wrangler login     # 会弹浏览器授权你的 Cloudflare 账号
# 先编辑 worker/wrangler.toml，取消 routes 注释并改成你的域名
npx wrangler deploy
```

手动部署后需要手动更新 GitHub Secret：

```bash
gh secret set WINGET_PROXY_PREFIX --body "https://gh.你的域名.com/" --repo 你的用户名/仓库名
```

**验证**：在浏览器或终端访问（注意是两层 URL 拼接）：

```bash
curl -sI "https://gh.你的域名.com/https://raw.githubusercontent.com/microsoft/winget-pkgs/master/README.md"
# 返回 HTTP 200 且 content-type 为文本即为成功；国内直连测试更有意义
```

### Step 2 · 填写配置

编辑根目录 [`config.json`](config.json)：

```jsonc
{
    "keepVersions": 3,          // 每个包在源里保留几个最新版本
    "upstreamRepo": "https://github.com/microsoft/winget-pkgs",
    // Pages 项目名 = 默认网址 <名字>.pages.dev；国内可达性见 Step 7 的域名提示
    "pagesProject": "winget-subset",
    "sourceName": "subset",              // 客户端 winget source add 时用的源名
    "publisherDn": "CN=winget-subset Source",
    "timestampServer": "http://timestamp.digicert.com"
}
```

> 🔒 **域名保密**：加速前缀只存在于 GitHub Secret `WINGET_PROXY_PREFIX` 与部署到 Pages 的产物中；
> 仓库内 `manifests/` 始终保存微软原始下载地址，构建时才注入你的域名，
> 因此公共仓库的提交历史、清单文件与 Actions 日志都不会暴露它。
> 
> 使用 `deploy_worker.py` 部署 Worker 时，Secret 会自动更新，无需手动配置。

**加速域名怎么配？**

使用 `deploy_worker.py` 部署 Worker 时会**自动配置**，无需手动操作。

手动配置时，值就是 **Step 1 中绑定给 Worker 的那个自定义子域名**，写成完整 URL、末尾带 `/`：

| Step 1 里给 Worker 绑定的域名 | Secret `WINGET_PROXY_PREFIX` 应填的值 |
|---|---|
| `gh.example.com` | `https://gh.example.com/` |

规则与注意事项：

- 必须**同时带** `https://` 前缀和**末尾的 `/`**，缺一个拼出来的安装器地址就无效；
- 不要用 `*.workers.dev` 地址 —— 国内被 DNS 污染基本不可达（见 Step 1 开头说明）；
- 之后换加速节点：运行 `python scripts/deploy_worker.py --domain 新域名 --repo owner/repo` 即可自动更新；
- 暂时不配置也能用：源照常工作，只是 GitHub 系安装器直连官方地址、没有加速效果。

### Step 3 · 挑选你的软件包

编辑 [`packages.txt`](packages.txt)，一行一个包 ID，`#` 后是注释：

```
Obsidian.Obsidian               # [proxy] GitHub 发布，自动加速
Tencent.WeChat.Universal        # [direct] 国内 CDN 直连
```

**不知道 ID 怎么办？** 在 Windows 终端搜索官方源：

```powershell
winget search 微信
# 输出的 ID 列（如 Tencent.WeChat.Universal）就是你要填的内容
```

也可以到 [winget-pkgs 的 manifests 目录](https://github.com/microsoft/winget-pkgs/tree/master/manifests)
按首字母浏览。注意：ID 必须真实存在于上游，拼写和大小写要一致。

### Step 4 · 生成签名证书（Windows）

管理员 PowerShell 中执行：

```powershell
cd <仓库目录>
.\scripts\new-signing-cert.ps1 -OutDir .
```

脚本会生成三个文件并在结尾打印后续步骤：

| 文件 | 用途 |
|---|---|
| `SIGNING_PFX.txt` | 私钥证书的 Base64 文本 → 待会儿配到 Secrets |
| `SIGNING_PFX_PASSWORD.txt` | 对应密码 → 同上 |
| `winget-subset.cer` | 公钥证书 → 备用；日常接入用 `scripts/install-source-cert.ps1` 从部署的 msix 直接提取，无需此文件 |

> ⚠️ `signing.pfx` 等于源的身份私钥，不要提交进 git、不要外传
> （`.gitignore` 已帮你排除）。

### Step 5 · 配置 GitHub Secrets

打开你的 GitHub 仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加四个：

| Secret 名称 | 值 |
|---|---|
| `SIGNING_PFX` | `SIGNING_PFX.txt` 文件的全部内容 |
| `SIGNING_PFX_PASSWORD` | `SIGNING_PFX_PASSWORD.txt` 的全部内容 |
| `CF_API_TOKEN` | 见下方创建方法 |
| `CF_ACCOUNT_ID` | 见下方查看方法 |
| `WINGET_PROXY_PREFIX` | （可选）加速域名前缀，如 `https://gh.你的域名.com/` —— 使用 `deploy_worker.py` 部署时会自动配置 |
| `WINGET_SOURCE_HOST` | （强烈建议）源站自定义域名，如 `winget.你的域名.com`。CI 端到端自测走该域名；不配置则用 pages.dev，会因不支持 Range 而自测失败 |

**创建 Cloudflare API Token**：dash.cloudflare.com 右上角头像 → **My Profile** → **API Tokens**
→ **Create Token** → 底部 **Create Custom Token**，按下面配置：

| 权限 | 用途 |
|---|---|
| `Account` + `Cloudflare Pages` + `Edit` | 自动创建 Pages 项目、wrangler 部署、自动绑定自定义源域名 |
| `Zone` + `Zone` + `Read` | 查找域名所属 Zone（建缓存规则与清缓存都依赖） |
| `Zone` + `Cache Rules` + `Edit` | 自动创建源域名缓存规则（解决 Pages 不支持 Range 请求） |
| `Zone` + `Cache Purge` + `Purge` | 每次部署后自动清边缘缓存 |

> ⚠️ **Zone Resources 必须选 Include → All zones**（或至少勾选你的域名所在 Zone）。
> 这是最常见的翻车点：只配权限、资源范围留空/选错账号，Token 能调通 API 却查不到任何
> Zone，工作流会在「部署到 Pages 并配置域名与缓存」一步报「Token 可见 Zone 不含源域名主域」。
> 创建完成后复制令牌。

**查看 Account ID**：左侧 **Workers & Pages** 概览页右栏，一串十六进制字符。

### Step 6 · 首次构建与发布

推送你的改动到 GitHub（例如 packages.txt 的增删），然后二选一触发：

- 仓库 **Actions** 页面 → 左侧 **sync-upstream** → **Run workflow**；
- 或什么都不做，等每天北京时间 05:00 的定时任务。

一次成功的运行包含这些步骤，重点看两处：

```
✓ 同步上游清单                          ← 每个包 OK/FAIL 有明细，FAIL 会汇总原因
✓ 构建源索引包
✓ 部署到 Pages 并配置域名与缓存        ← 自动建项目/绑源域名/建缓存规则/清边缘缓存
✓ 端到端自测                           ← 自动导入证书→添加源→经代理真实安装 Obsidian 并校验哈希
✓ 提交同步结果                         ← manifests/ 的变更自动 commit 回仓库
```

**端到端自测通过 = 整条链路（同步、索引、签名、Pages、Worker 加速、哈希校验）全部健康。**

### Step 7 · 在自己的电脑上接入

> 📌 **给 Pages 绑上自定义域名**：`*.pages.dev` 与 `*.workers.dev` 同病相连，国内基本不可达。
> 配好 Secret `WINGET_SOURCE_HOST`（见 Step 5）后 CI 会在部署时**自动绑定**该子域名 ——
> 前提是域名的 Zone 托管在同一个 Cloudflare 账号（此时连 CNAME 记录都自动创建），
> 证书签发需数分钟，端到端自测会自动轮询等待。
> 想手动操作也可以：控制台 → **Workers & Pages** → 你的 Pages 项目 →
> **Custom domains** → 添加一个子域名（例如 `winget.你的域名.com`），DNS 与证书自动配置。
> 下文源地址把 `winget-subset.pages.dev` 全部替换成你的域名即可。

> ⚠️ **还必须给源域名配置一条缓存规则，否则 winget 无法添加本源** ——
> CI 会自动创建，详见下方「给源域名配置缓存规则」。

#### 给源域名配置缓存规则（CI 自动完成）

**为什么必须配**：winget 拉取 `source.msix` 索引包时使用 HTTP `Range` 头分段下载，
而 Cloudflare Pages 的静态资源不支持 Range 请求 —— 收到带 `Range` 的请求会返回
`200` 全量响应而非 `206 Partial Content`，导致 winget 解析失败。更坑的是它报出的错误
极具误导性：表面是 `404 Not Found` 或数据完整性错误 `0x8051100F`，真实原因完全看不到。
解决办法是让请求经 Cloudflare 边缘缓存转发：边缘节点代替 Pages 应答 Range 请求，正常返回 206。

**自动配置**：只要配好 Secret `WINGET_SOURCE_HOST`，且 `CF_API_TOKEN` 含
`Zone + Cache Rules + Edit` 权限（见 Step 5），工作流每次运行都会自动为源域名创建这条规则，
已存在则跳过。等价于控制台里手动创建：

> 主机名等于 `winget.你的域名.com` → 缓存资格「符合缓存条件」→
> 边缘 TTL「忽略源站并使用此 TTL」2 小时

**手动配置步骤（本地部署或 CI 报权限错误时备用）**（英文界面为主，括号内是控制台切中文后的对应文案）：

1. 登录 [dash.cloudflare.com](https://dash.cloudflare.com)，在首页点击进入托管
   `winget.你的域名.com` 的那个主域名（Zone）的管理页；
2. 左侧菜单 **Caching（缓存）** → **Cache Rules**；
3. 点击 **Create rule（创建规则）**，规则名随意（如 `winget-source`）；
4. 在「If incoming requests match（如果传入请求匹配）」中填写三个字段：
   - Field（字段）：`Hostname（主机名）`
   - Operator（运算符）：`equals（等于）`
   - Value（值）：`winget.你的域名.com`（就是绑定给 Pages 的那个子域名）
5. 在「Cache eligibility（缓存资格）」中选择 **Eligible for cache（符合缓存条件）**；
6. 向下展开「Edge TTL（边缘 TTL）」，选择 **Ignore origin and use this TTL（忽略源站并使用此 TTL）**，
   数值填 `2` 小时；
7. 其余选项全部保持默认，点击 **Deploy（部署）** 保存生效。

**验证规则是否生效**：

```powershell
curl.exe -sI -H "Range: bytes=0-1023" https://winget.你的域名.com/source.msix
```

连续执行两次，第二次应满足三点：状态码 `HTTP/2 206`、响应头出现
`Content-Length: 1024`、`cf-cache-status` 为 `HIT`。
若仍是 `200`，多半是主机名填错或第 5 步没有选「符合缓存条件」。

**副作用与维护**：边缘缓存意味着每次部署后，客户端最多延迟 2 小时才能读到新索引；
但 CI 在部署完成后会自动清除该域名的边缘缓存，走 CI 无需关心。
若你在本地手动构建部署又想立即生效，可在
**Caching → Overview（概述） → Purge Cache（清除缓存）** 中一键清除，
或在客户端执行 `winget source update --name subset` 强制刷新。

一键信任源签名证书（每台电脑一次性；管理员运行对所有用户生效，非管理员则仅当前用户）：

```powershell
.\scripts\install-source-cert.ps1 -SourceUrl https://winget.你的域名.com
# 已有本地 msix 文件时免下载:
.\scripts\install-source-cert.ps1 -LocalMsix .\source.msix
```

脚本从源地址下载 `source.msix`、提取签名证书并导入 `Root` + `TrustedPeople` 两个信任存储，
导入后自动校验签名链。若 `winget source add` 报 `0x8a15003f 源数据已损坏或被篡改`，
说明证书尚未导入，跑一次本脚本即可。

添加源并开始使用：

```powershell
# 添加（与官方 winget 源并存，互不影响；首次会提示信任协议）
winget source add --name subset --arg https://winget.你的域名.com/ --type "Microsoft.PreIndexed.Package"

# 安装（记得指定 --source subset）
winget install --id Obsidian.Obsidian --source subset

# 一键升级子集内全部软件
winget upgrade --all --source subset

# 浏览源里有哪些包
winget search --source subset

# 不想用了随时移除
winget source remove --name subset
```

---

## 四、日常维护

| 想做什么 | 操作 |
|---|---|
| 增删软件 | 编辑 `packages.txt` → 推送，或手动触发工作流 |
| 加速节点失效 | 运行 `python scripts/deploy_worker.py --domain 新域名 --repo owner/repo` 自动更新 |
| 只想装最新版 | `keepVersions` 改为 1 |
| 更换 Pages 网址 | 改 `pagesProject` 后重跑工作流，CI 自动创建新项目并绑回源域名（旧项目可在控制台删除） |
| 手动强制刷新源 | `winget source update --name subset` |

每次运行的详细同步报告在 Actions 日志里，也会随产物输出 `dist/sync-report.json`
（本地运行时），记录每个包保留了哪些版本、改写了多少条地址、哪些包失败及原因。

## 五、进阶玩法

**优选 Cloudflare 节点提升速度**：绑定自有域名后若仍觉得慢，可用社区"优选 IP"工具
测出对你运营商最快的 Cloudflare 入口 IP，在路由器或本机 hosts 中把两个自定义子域
指向该 IP。这是纯客户端侧优化，无需改动本项目任何配置。

**国内网络本地手动跑同步**（不想等 CI）：同步脚本零依赖，任意平台可用：

```bash
python3 scripts/sync_manifests.py              # 按 config.json 同步
python3 scripts/sync_manifests.py --keep 1     # 临时只留最新一版
```

本机连不上 GitHub 时不用另配代理 —— 把上游指到你的 Worker 即可（它支持 git 智能协议转发）：

```bash
python3 scripts/sync_manifests.py \
    --upstream "https://<你的worker域名>/https://github.com/microsoft/winget-pkgs"
```

同样手法也能直接 clone 任何 GitHub 仓库：

```bash
git clone https://<你的worker域名>/https://github.com/owner/repo.git
```

Windows 上手动构建源包（调试时用）：

```powershell
.\scripts\build_source.ps1                # 需要 SIGNING_PFX/PASSWORD 环境变量
.\scripts\build_source.ps1 -SkipSigning   # 未签名产物仅供调试，客户端无法加载
```

构建时如需加速前缀，先设置环境变量再执行（只影响 dist/pages 部署副本，不改动仓库清单）：

```powershell
$env:WINGET_PROXY_PREFIX = 'https://gh.你的域名.com/'
```

## 六、故障排查

| 现象 | 原因与处理 |
|---|---|
| 运行脚本报"不是数字签名 / 无法加载" | 从 zip 解压的文件带下载标记，`RemoteSigned` 策略会拒绝。在仓库目录执行 `Get-ChildItem -Recurse \| Unblock-File`，或右键该脚本 → 属性 → 勾选"解除锁定" |
| 构建报"未找到 Windows SDK / makeappx" | 脚本会自动从微软官方 NuGet 包拉取打包工具（需联网，仅首次约几十秒，之后同机复用、CI 也已跨运行缓存）；完全离线的机器请安装 Windows SDK，或设置环境变量 `WINGET_SDK_TOOLS_DIR` 指向工具目录 |
| 安装时报 SHA256 校验失败 | 加速节点返回了错误页而非安装包（公共代理常见）。换 Worker 域名，或临时把该包注释掉/标记直连 |
| `winget source add` 报源不受信任 / 数据完整性错误 `0x8a15003f` | 该电脑没导入公钥证书。管理员运行 `.\scripts\install-source-cert.ps1 -SourceUrl <源地址>` 一键信任（见 Step 7） |
| `winget source add` 报 Not found (404)，且源站文件用浏览器能正常下载 | Pages 静态资源不支持 Range 请求，winget 拿不到 206 分段响应所致（真实错误常被掩盖）。CI 配好 `WINGET_SOURCE_HOST` 会自动建缓存规则（Token 需含 `Zone + Cache Rules + Edit`）；排查按 Step 7「给源域名配置缓存规则」逐项检查 |
| Actions 在「部署到 Pages 并配置域名与缓存」步骤报 Zone 不匹配 | `CF_API_TOKEN` 作用域没覆盖域名：权限需含 `Zone + Zone + Read`、`Zone + Cache Rules + Edit`、`Zone + Cache Purge + Purge`，且 **Zone Resources 建议 Include → All zones**（只勾了 Pages 相关权限或选错账号/Zone 都会导致查不到）；同时确认域名已添加进该 Cloudflare 账号并激活 |
| Actions 里某包 FAIL: missing | 上游无此 ID 或已改名/下架，核对 `winget search` 结果与 sync-report.json |
| Actions 里某包 FAIL: no-version | 上游该包暂无数字版本目录（常见于只有预览版的包），CI 会自动重试次日数据 |
| Pages 已更新但搜索结果没变 | 客户端缓存，执行 `winget source update --name subset` |
| `winget upgrade` 报 `0x8a15003f 源数据已损坏或被篡改`（升级列表能正常列出） | 本机源索引缓存过期：旧索引里的清单哈希与服务器最新清单不一致（部署更新后必现）。执行 `winget source update --name subset` 强制刷新缓存即可，非数据损坏 |
| 定时任务没跑 | 确认 Actions 已启用、工作流在默认分支上；60 天无仓库活动时 GitHub 会暂停 schedule，手动触发一次即恢复 |
| workers.dev / pages.dev 地址打不开 | 正常现象，这两个默认域名在国内被 DNS 污染。按 Step 1 / Step 7 绑定自有域名后使用 |

## 七、安全模型

- 清单内容逐字节取自微软官方仓库，本项目**只修改 InstallerUrl 的主机前缀**这一处；
- 所有安装器的 `InstallerSha256` 保持原值 —— Worker、Pages、网络链路上的任何一方
  若篡改文件字节，WinGet 都会校验失败并拒绝安装；
- 源索引包由你自己生成的证书签名，私钥只存在于你的 GitHub Secrets 和本地导出文件中；
- 因此**信任边界就是你自己的 Cloudflare/GitHub 账号**，不依赖任何第三方镜像站点的善意。

## 八、致谢与许可

- [cloudflightio/winget-pkgs](https://github.com/cloudflightio/winget-pkgs)（Apache-2.0）：
  index.db 表结构、AppxManifest 模板与打包思路来自该项目，详见 [NOTICE.md](NOTICE.md)；
- [hunshcn/gh-proxy](https://github.com/hunshcn/gh-proxy)：加速方案灵感来源，本项目 Worker 为其精简重写版；
- [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs)：所有清单内容的唯一来源。

欢迎按自己的需求改造：换托管平台（OSS/自有服务器只需静态托管 `dist/pages/` 内容）、
换加速服务（任何"URL 前缀"型代理都兼容 `proxyPrefix` 字段）。
