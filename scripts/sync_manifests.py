#!/usr/bin/env python3
"""同步 microsoft/winget-pkgs 指定包的最新清单到本地 manifests/。

用法:
    python3 scripts/sync_manifests.py [--config config.json] [--packages packages.txt]
        [--upstream <git-url|本地路径>] [--keep N] [--out manifests]
        [--prefix <url>] [--rewrite-dir <目录>]

安全约定: 入库清单始终保留微软原始 InstallerUrl，真实加速域名不进 git；
部署前由 build_source.ps1 以 --rewrite-dir 对 dist/pages 副本注入前缀。
退出码: 0 全部成功；2 存在失败的包；1 参数/环境错误。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 非 UTF-8 控制台（如 GitHub windows runner 的 cp1252）打印中文会崩溃
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and getattr(_stream, 'reconfigure', None):
        _stream.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / '.cache' / 'winget-pkgs'
BRANCH_KEY = 'winget-subset.branch'

# 合法版本目录名：数字点分（可带 -后缀/+元数据），排除 Pre-Release/Locale 等非版本目录
VERSION_RE = re.compile(r'^\d+(\.\d+)*([\-+].+)?$')
VERSION_CORE_RE = re.compile(r'^(\d+(?:\.\d+)*)')

PROXY_HOST_SUFFIXES = ('github.com', 'githubusercontent.com', 'codeload.github.com')

# 仅匹配行内纯 URL 或带引号 URL 形式的 InstallerUrl；字节级处理保留原缩进与换行（CRLF 安全）
INSTALLER_URL_RE = re.compile(
    rb'^(\s*(?:-\s*)?InstallerUrl:\s*)"?(https://[^\s"\'#]+)"?(\r?\n?)$', re.M)


def loadConfig(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def readPackages(path):
    # 忽略空行与 # 注释（支持行内注释）
    return [line for raw in Path(path).read_text(encoding='utf-8').splitlines()
            if (line := raw.split('#', 1)[0].strip())]


def idToManifestPath(pkgId):
    # BellSoft.LibericaJDK.25.Full -> b/BellSoft/LibericaJDK/25/Full（首段取首字母小写单字符目录）
    segments = pkgId.split('.')
    return '/'.join(['manifests', segments[0][0].lower()] + segments)


def runGit(args, cwd=None, check=True):
    return subprocess.run(
        ['git'] + args, cwd=cwd, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def detectBranch(upstream):
    out = subprocess.run(
        ['git', 'ls-remote', '--symref', str(upstream), 'HEAD'],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout
    match = re.search(r'refs/heads/(\S+)', out)
    return match.group(1) if match else 'master'


def ensureUpstreamCache(upstream, sparsePaths):
    """保证缓存仓库最新：origin 变更时整仓重建，否则增量更新稀疏集并 reset。"""
    upstream = str(upstream).rstrip('/')
    if (CACHE_DIR / '.git').exists():
        currentOrigin = runGit(['config', '--get', 'remote.origin.url'],
                               cwd=CACHE_DIR, check=False).stdout.strip()
        if currentOrigin != upstream:
            shutil.rmtree(CACHE_DIR)
        else:
            # 分支名缓存在仓库本地配置，省去每次 ls-remote 的网络往返
            branch = runGit(['config', '--get', BRANCH_KEY],
                            cwd=CACHE_DIR, check=False).stdout.strip() \
                or detectBranch(upstream)
            # 用 set 整体替换稀疏集，顺带清掉已移除包的陈旧路径
            runGit(['sparse-checkout', 'set', '--cone'] + sparsePaths,
                   cwd=CACHE_DIR, check=False)
            runGit(['fetch', '--depth', '1', 'origin', branch], cwd=CACHE_DIR)
            runGit(['reset', '--hard', 'FETCH_HEAD'], cwd=CACHE_DIR)
            return

    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)

    cloneArgs = ['clone', '--filter=blob:none', '--no-checkout', '--depth', '1',
                 '--branch', 'master', upstream, str(CACHE_DIR)]
    branch = 'master'
    if runGit(cloneArgs, check=False).returncode != 0:
        # 上游默认分支可能为 main
        cloneArgs[cloneArgs.index('master')] = 'main'
        branch = 'main'
        runGit(cloneArgs)
    runGit(['sparse-checkout', 'init', '--cone'], cwd=CACHE_DIR)
    runGit(['sparse-checkout', 'set', '--cone'] + sparsePaths, cwd=CACHE_DIR)
    runGit(['checkout', branch], cwd=CACHE_DIR)
    runGit(['config', BRANCH_KEY, branch], cwd=CACHE_DIR)


def versionSortKey(name):
    coreMatch = VERSION_CORE_RE.match(name)
    core = tuple(int(x) for x in coreMatch.group(1).split('.'))
    return (core, name[coreMatch.end(1):])


def latestVersions(pkgDir, keep):
    versions = [p.name for p in pkgDir.iterdir()
                if p.is_dir() and VERSION_RE.match(p.name)]
    versions.sort(key=versionSortKey, reverse=True)
    return versions[:keep]


def isProxiedHost(url):
    host = url.decode('ascii', 'ignore').split('/', 3)[2].lower()
    return any(host == s or host.endswith('.' + s) for s in PROXY_HOST_SUFFIXES)


def rewriteFile(filePath, prefixBytes):
    """对单个 installer.yaml 做 InstallerUrl 前缀改写；返回改写条数。"""
    data = filePath.read_bytes()
    count = 0

    def replace(match):
        nonlocal count
        head, url, eol = match.group(1), match.group(2), match.group(3)
        if isProxiedHost(url):
            count += 1
            return head + prefixBytes + url + eol
        return match.group(0)

    rewritten = INSTALLER_URL_RE.sub(replace, data)
    if count:
        filePath.write_bytes(rewritten)
    return count


def rewriteTree(rootDir, prefixBytes):
    """对目录树内全部清单 yaml 注入前缀；返回 (文件数, 条数)。
    
    同时处理多文件清单的 *.installer.yaml 和 singleton 的 *.yaml。
    """
    fileCount = urlCount = 0
    for yamlFile in sorted(rootDir.rglob('*.yaml')):
        n = rewriteFile(yamlFile, prefixBytes)
        if n:
            fileCount += 1
            urlCount += n
    return fileCount, urlCount


def syncPackage(pkgId, cacheRoot, outRoot, keep):
    entry = {'id': pkgId, 'versions': [], 'status': 'ok'}
    srcPkg = cacheRoot / idToManifestPath(pkgId)
    # outRoot 即 manifests 目录本身，拼接时不含 manifests 前缀
    dstPkg = outRoot.joinpath(*idToManifestPath(pkgId).split('/')[1:])

    if not srcPkg.is_dir():
        return {**entry, 'status': 'missing'}
    chosen = latestVersions(srcPkg, keep)
    if not chosen:
        return {**entry, 'status': 'no-version'}

    if dstPkg.exists():
        shutil.rmtree(dstPkg)
    for ver in chosen:
        shutil.copytree(srcPkg / ver, dstPkg / ver)
        entry['versions'].append(ver)
    return entry


def printSummary(report):
    print('\n===== 同步结果 =====')
    for item in report['packages']:
        flag = 'OK  ' if item['status'] == 'ok' else 'FAIL'
        versions = ', '.join(item['versions']) or '-'
        print(f"[{flag}] {item['id']:<32} 版本: {versions}")
    failed = [i['id'] for i in report['packages'] if i['status'] != 'ok']
    print(f"\n合计: {len(report['packages'])} 个包, "
          f"成功 {len(report['packages']) - len(failed)}, 失败 {len(failed)}")
    if failed:
        print('失败列表:', ', '.join(failed))


def main():
    parser = argparse.ArgumentParser(description='同步 winget 子集清单')
    parser.add_argument('--config', default=str(REPO_ROOT / 'config.json'))
    parser.add_argument('--packages', default=None)
    parser.add_argument('--upstream', default=None)
    parser.add_argument('--keep', type=int, default=None)
    parser.add_argument('--out', default=None)
    parser.add_argument('--prefix', default=None,
                        help='代理前缀；优先级高于环境变量与配置文件')
    parser.add_argument('--rewrite-dir', dest='rewriteDir', default=None,
                        help='仅对目录树内 *.installer.yaml 注入前缀后退出（用于部署副本）')
    args = parser.parse_args()

    try:
        config = loadConfig(args.config)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'读取配置失败: {exc}')
        return 1

    # 前缀来源优先级: CLI > 环境变量(Secret) > config.json；真实域名不写入任何入库文件
    proxyPrefix = (
        args.prefix
        or os.environ.get('WINGET_PROXY_PREFIX', '').strip()
        or str(config.get('proxyPrefix') or '').strip()
    )

    if args.rewriteDir:
        if not proxyPrefix:
            print('未提供代理前缀: 请用 --prefix、环境变量 WINGET_PROXY_PREFIX 或 config.json')
            return 1
        root = Path(args.rewriteDir)
        if not root.is_dir():
            print(f'目录不存在: {root}')
            return 1
        fileCount, urlCount = rewriteTree(root, proxyPrefix.encode())
        print(f'前缀注入完成: {fileCount} 个文件, {urlCount} 条 InstallerUrl')
        return 0

    packagesFile = Path(args.packages or REPO_ROOT / 'packages.txt')
    upstream = args.upstream or config.get('upstreamRepo')
    keep = args.keep or int(config.get('keepVersions', 3))
    outRoot = Path(args.out) if args.out else REPO_ROOT / 'manifests'

    packages = readPackages(packagesFile)
    if not packages:
        print('包列表为空，请检查 packages.txt')
        return 1

    print(f"包数量: {len(packages)} | 保留版本数: {keep} | "
          f"部署时代理注入: {'已启用' if proxyPrefix else '(未启用)'}")

    sparsePaths = [idToManifestPath(p) for p in packages]
    try:
        ensureUpstreamCache(upstream, sparsePaths)
    except subprocess.CalledProcessError as exc:
        print(f'克隆/更新上游仓库失败:\n{exc.stdout}')
        return 1

    outRoot.mkdir(parents=True, exist_ok=True)
    report = {
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'upstream': str(upstream),
        'keepVersions': keep,
        'proxyEnabled': bool(proxyPrefix),
        'packages': [syncPackage(p, CACHE_DIR, outRoot, keep) for p in packages],
    }
    distDir = REPO_ROOT / 'dist'
    distDir.mkdir(exist_ok=True)
    (distDir / 'sync-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    printSummary(report)
    failed = [i['id'] for i in report['packages'] if i['status'] != 'ok']
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
