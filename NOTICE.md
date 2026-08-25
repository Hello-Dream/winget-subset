# NOTICE — 第三方素材与代码来源说明

本仓库以下内容移植自 [cloudflightio/winget-pkgs](https://github.com/cloudflightio/winget-pkgs)（Apache License 2.0），
并按本项目需求做了修改：

- `source-tpl/`：源索引包的 `AppxManifest.xml`、图标资源（`Images/`、`Assets/`）
- `scripts/build_index.py` 中嵌入的 SQLite 表结构（源自其 `index.db.sql`，schema v1.6）
- 打包流程思路（makeappx → source.msix）

其余代码为本项目原创。Apache License 2.0 全文见
https://www.apache.org/licenses/LICENSE-2.0
