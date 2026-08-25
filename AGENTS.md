# AGENTS.md

## 项目本质

不是常规应用，而是一条「数据管道 + 静态站点」：从 microsoft/winget-pkgs 部分克隆同步子集清单到 `manifests/` → 构建 SQLite 索引并打包签名为 `source.msix` → 发布到 Cloudflare Pages；另有独立部署的 Cloudflare Worker（`worker/`）做 GitHub 下载加速。无测试框架、无 lint、无包管理器：Python 仅标准库，Worker 为单文件 JS。

## 常用命令

```bash
# 同步清单（任意平台，需 git；报告写入 dist/sync-report.json）
python3 scripts/sync_manifests.py            # 退出码: 0=全部成功, 2=部分包失败(missing/no-version), 1=环境错误
python3 scripts/sync_manifests.py --keep 1   # CLI 参数覆盖 config.json

# 单独构建索引（任意平台）
python3 scripts/build_index.py --manifests manifests --output dist/stage/Public/index.db
```

```powershell
# 打包 source.msix（仅 Windows；本机缺 makeappx/signtool 时自动从微软 NuGet 包拉取）
.\scripts\build_source.ps1 -SkipSigning      # 无 SIGNING_PFX 环境变量时必须加此开关，未签名产物仅供调试

# 客户端一键信任源签名证书（每台使用该源的机器一次；管理员导入 LocalMachine，非管理员回退 CurrentUser）
.\scripts\install-source-cert.ps1 -SourceUrl https://<源域名>   # 或 -LocalMsix <本地 msix> 免下载
```

完整链路验证只有一条路：GitHub Actions 手动触发 `sync-upstream` 工作流（windows-latest），它会 同步 → 构建 → 部署 Pages 并自动配置（建项目/绑源域名/缓存规则/清边缘缓存）→ 端到端自测（真实经代理安装并卸载 Obsidian）→ 把 `manifests/` 变更以 `chore(sync)` 自动提交回 main。不要"清理"这个看似特殊的自测步骤 —— 它是整条链路的唯一健康检查。

## 安全红线（改清单相关代码前必读）

- **仓库内 `manifests/` 永远保持微软原始 InstallerUrl**。代理前缀只在构建阶段注入 `dist/pages/` 副本（由 `build_source.ps1` 调 `sync_manifests.py --rewrite-dir` 完成）。真实加速域名不得写入 `config.json` 的 `proxyPrefix`、`manifests/` 或任何入库文件 —— 公共仓库提交历史会泄露它；域名只应出现在 GitHub Secret `WINGET_PROXY_PREFIX` 与部署产物中。
- 清单内容只允许修改 InstallerUrl 的主机前缀这一处；`InstallerSha256` 必须保持上游原值，哈希校验是整个信任模型的基础。
- `*.pfx`、`SIGNING_PFX*.txt` 已被 .gitignore 排除，不要提交或外传。

## CI / Cloudflare 自动化坑（改 sync.yml 前必读）

- 部署后置配置全自动：建 Pages 项目 → `wrangler@4 pages deploy` → 绑源域名（Secret `WINGET_SOURCE_HOST`，同账号 Zone 时 CNAME 自动创建）→ 建缓存规则（描述 `managed-by-winget-subset-ci` 是幂等跳过标记）→ purge 边缘缓存。`CF_API_TOKEN` 需四组权限：Account/Pages/Edit、Zone/Zone/Read、Zone/Cache Rules/Edit、Zone/Cache Purge/Purge，且 **Zone Resources 必须选 All zones** —— 漏配是最高频翻车点，报「Token 可见 Zone 不含源域名主域」。
- **日志防泄露**：GitHub 只按 Secret 原值做精确遮蔽，从 Secret 派生的字符串（子域、主域）不在遮蔽范围 —— 输出前必须 `Write-Output "::add-mask::<派生串>"`，报错文案一律不回显域名（已发生过泄露事故）。
- 清缓存端点是 `POST /zones/{id}/purge_cache`；写成 `/cache/purge` 会报 7000 "No route for that URI"（曾踩坑）。
- 缓存规则走 Ruleset API：`PUT /zones/{id}/rulesets/phases/http_request_cache_settings/entrypoint` 会**整体替换**该 phase 的全部规则 —— 必须先 GET 回写已有规则（剥离 id/version 等只读字段）；GET 的 result 为 null 时 `@($entry.result.rules)` 会混入一个全 null 的假规则，需按 expression 过滤。
- SDK 打包工具经 `actions/cache` 跨运行缓存，路径即环境变量 `WINGET_SDK_TOOLS_DIR`（`build_source.ps1` 既用它查找工具也用它作下载落盘目录）；改动工具获取逻辑时勿破坏该约定。
- wrangler 版本钉死在 `wrangler@4`，升级属有意变更而非随手改动。

## 验证方式（无测试框架）

- Python 改动：`python -m py_compile scripts/*.py` + 对本地 `manifests/` 实跑一次 `build_index.py`（秒级完成）。
- PowerShell 改动：用 `[System.Management.Automation.Language.Parser]::ParseFile()` 检查零错误，再本地跑通 `build_source.ps1 -SkipSigning` 全链路（同机二次起约几秒）。
- 前缀注入可离线验证：把 `manifests/` 复制到临时目录后执行 `sync_manifests.py --rewrite-dir <副本> --prefix https://example.com/`——**切勿对仓库内 manifests/ 直接执行**（会污染原始地址）。

## 结构与约定

- **Cloudflare Pages 静态资源不支持 Range 请求**（对 `Range` 头回 200 全量而非 206），而 winget 流式拉取 `source.msix` 强制依赖 206，否则报出误导性错误（表面 404 实为解析失败 0x8051100F）。因此源域名必须配 Cache Rule（符合缓存条件 + 忽略源站 TTL，CI 每次部署自动创建），CI 自测域名走 Secret `WINGET_SOURCE_HOST`（不入库），部署后有自动 purge 步骤。排查此类问题用 `--verbose-logs` 看真实失败原因。
- **客户端陈旧缓存**：`source-tpl/_headers` 给 `source.msix`/`manifests/*` 设 `Cache-Control: max-age=0, must-revalidate`（配合 ETag 强制重新验证），防止 winget 的隔离 WinINet 缓存（`%LOCALAPPDATA%\Packages\Microsoft.DesktopAppInstaller_*\AC\INetCache`）按 max-age 把旧索引/清单缓存数小时——曾导致部署后 `source update` 仍命中旧包（0x8a15003f / 0x80070032 表象）。`_headers` 只复制进 `dist/pages`，不进 msix；边缘缓存由 Cache Rule 的 Edge TTL 独立控制不受影响。若客户端仍陈旧，删除该 `AC\INetCache` 目录后重新 `source update`。
- 新版 winget 添加 PreIndexed 源时先探测 `source2.msix` 再回退 `source.msix`，且最终只报**第一个**异常 —— 日志里的 404 可能只是表象，要看每个 location 各自的错误。
- 客户端 `winget source add` 报 `0x8a15003f 源数据已损坏或被篡改` = 该机未信任自签签名证书，跑 `scripts/install-source-cert.ps1` 导入 `Root` + `TrustedPeople`（MSIX 包签名校验走 TrustedPeople，`Get-AuthenticodeSignature` 只查 Root，二者缺一不可，故脚本两者都导）。
- `packages.txt`：一行一个包 ID，`#` 为行内注释；`[proxy]`/`[direct]` 标记只是给人看的说明，脚本不解析。
- ID → 路径映射规则：`BellSoft.LibericaJDK.25.Full` → `manifests/b/BellSoft/LibericaJDK/25/Full`（首段取首字母小写单字符目录）。
- 每包保留最新 `keepVersions` 个数字版本目录（正则过滤 Locale 等非版本目录）；CI 中个别包 FAIL 属预期，次日自动重试。
- `scripts/mini_yaml.py` 是内置极简 YAML 解析器，刻意避免 PyYAML 依赖 —— 不要随手引入第三方依赖。
- `.cache/winget-pkgs` 是 blob:none + sparse-checkout 的上游缓存，origin 变化时整仓重建；国内连不上 GitHub 可把 `--upstream` 指向 Worker 域名中转（支持 git 智能协议）。
- Python 脚本强制 stdout/stderr 为 UTF-8（Windows runner 默认 cp1252，打印中文会崩溃）；新增脚本输出中文时需同样处理。
- Worker 不参与 CI：改动 `worker/src/index.js` 后需手动 `npx wrangler deploy` 或控制台粘贴部署；域名 allowlist 在文件顶部 `ALLOWED_HOST_SUFFIXES`。
- 提交信息遵循 `<type>(<scope>): 中文描述`（见 git log）；main 分支存在 github-actions[bot] 的定时自动提交，属正常现象。
