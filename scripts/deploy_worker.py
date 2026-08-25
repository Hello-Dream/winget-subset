#!/usr/bin/env python3
"""部署 gh-proxy Worker 并自动更新 GitHub Secret WINGET_PROXY_PREFIX。

用法:
    python3 scripts/deploy_worker.py --domain gh.example.com [--repo owner/repo]

环境变量:
    GH_TOKEN: GitHub Personal Access Token（需 repo 权限）
    GITHUB_REPOSITORY: 仓库全名（CI 中自动设置；本地需传 --repo）

退出码: 0 成功；1 失败。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_DIR = REPO_ROOT / 'worker'
WRANGLER_TOML = WORKER_DIR / 'wrangler.toml'


def deployWorker(domain):
    """部署 Worker 并绑定自定义域名。"""
    if not WRANGLER_TOML.is_file():
        print(f'错误: {WRANGLER_TOML} 不存在')
        return False

    toml_content = WRANGLER_TOML.read_text('utf-8')

    # 移除旧的 routes 注释块
    lines = []
    in_routes_block = False
    for line in toml_content.splitlines():
        stripped = line.strip()
        if stripped.startswith('# routes = ['):
            in_routes_block = True
            continue
        if in_routes_block:
            if stripped.startswith('# ]'):
                in_routes_block = False
            continue
        if stripped.startswith('# custom_domain') or stripped.startswith('#     { pattern'):
            continue
        lines.append(line)

    # 添加新的 routes 配置
    routes_config = f'''
# 自动配置: 由 deploy_worker.py 生成
routes = [
    {{ pattern = "{domain}", custom_domain = true }},
]'''
    lines.append(routes_config)

    WRANGLER_TOML.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'wrangler.toml 已更新: routes -> {domain}')

    # 执行 wrangler deploy
    print(f'部署 Worker 到 {domain}...')
    result = subprocess.run(
        ['npx', 'wrangler', 'deploy'],
        cwd=str(WORKER_DIR),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f'wrangler deploy 失败:\n{result.stderr}')
        return False

    print(f'Worker 部署成功: https://{domain}/')
    return True


def updateGitHubSecret(repo, domain):
    """更新 GitHub Secret WINGET_PROXY_PREFIX。"""
    proxy_prefix = f'https://{domain}/'
    print(f'更新 GitHub Secret WINGET_PROXY_PREFIX -> {proxy_prefix}')

    result = subprocess.run(
        ['gh', 'secret', 'set', 'WINGET_PROXY_PREFIX', '--body', proxy_prefix, '--repo', repo],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f'gh secret set 失败:\n{result.stderr}')
        return False

    print('GitHub Secret 更新成功')
    return True


def main():
    parser = argparse.ArgumentParser(description='部署 gh-proxy Worker 并自动配置')
    parser.add_argument('--domain', required=True, help='Worker 自定义域名（如 gh.example.com）')
    parser.add_argument('--repo', help='GitHub 仓库全名（如 owner/repo），CI 中自动设置')
    args = parser.parse_args()

    repo = args.repo
    if not repo:
        repo = Path('.').resolve().name  # 尝试从当前目录推断
        print(f'未指定 --repo，使用推断值: {repo}')

    # 部署 Worker
    if not deployWorker(args.domain):
        return 1

    # 更新 GitHub Secret
    if not updateGitHubSecret(repo, args.domain):
        return 1

    print(f'\n完成! 下次 CI 运行将自动使用 https://{args.domain}/ 作为代理前缀')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
