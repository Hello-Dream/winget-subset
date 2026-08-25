#!/usr/bin/env python3
"""离线校验 winget 子集清单与索引，无需网络/管理员/winget。

用途:
    python3 scripts/validate_manifests.py [--manifests manifests]
        [--stage dist/stage/Public/index.db] [--pages dist/pages]
        [--check-index] [--validate-manifests]

子能力:
    1) --check-index: 打开 index.db，断言 canary（默认 Obsidian.Obsidian）已进索引，
       并逐一核对索引 pathpart 指向的清单文件确实存在于 dist/pages/manifests/。
       捕获「同步静默跳过某包」或「路径/大小写错位」这类系统性 bug。
    2) --validate-manifests: 遍历 manifests/，对每个版本做 winget 式多文件合并，
       检测会导致 winget 报 0x8a150004「Opening manifest failed」的畸形结构
       （如 AppsAndFeaturesEntries / Installers 列表里混进空对象/标量）。

默认（不带参数）两项都跑。退出码: 0 通过；1 发现阻断性问题；2 参数/环境错误。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mini_yaml import load as safe_load, YamlSyntaxError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = __import__('re').compile(r'^\d+(\.\d+)*([\-+].+)?$')

# 期望为「列表且元素必须是映射」的键（winget 合并这些字段时若混入空对象/标量会炸）
LIST_OF_DICT_KEYS = ('Installers', 'AppsAndFeaturesEntries')


def loadConfig(path):
    import json
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def findVersionDirs(manifestsRoot):
    result = []
    for dirPath in manifestsRoot.rglob('*'):
        if dirPath.is_dir() and VERSION_RE.match(dirPath.name) \
                and list(dirPath.glob('*.yaml')):
            result.append(dirPath)
    return sorted(result)


def mergeManifestData(yamlFiles):
    merged = {}
    for f in yamlFiles:
        data = safe_load(f.read_bytes().decode('utf-8-sig')) or {}
        for key in ('PackageIdentifier', 'PackageVersion', 'PackageName', 'Publisher',
                    'Moniker', 'Tags', 'Commands', 'Installers', 'AppsAndFeaturesEntries'):
            if key in data:
                merged[key] = data[key]
    return merged


def iterNested(obj, path):
    """递归产出 (路径, 值)；用于定位畸形结构。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from iterNested(v, path + [str(k)])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iterNested(v, path + [f'[{i}]'])
    else:
        yield path, obj


def checkManifestStructures(merged, relLabel):
    """返回问题列表；空列表表示结构健康。"""
    problems = []
    if not isinstance(merged, dict):
        return [f'{relLabel}: 顶层不是映射']
    for req in ('PackageIdentifier', 'PackageVersion'):
        if req not in merged or not merged.get(req):
            problems.append(f'{relLabel}: 缺少必需字段 {req}')

    for key in LIST_OF_DICT_KEYS:
        value = merged.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            problems.append(f'{relLabel}: {key} 应为列表，实为 {type(value).__name__}')
            continue
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                problems.append(
                    f'{relLabel}: {key}[{i}] 应为映射，实为 '
                    f'{type(item).__name__}（空对象/标量混入列表，会触发 0x8a150004）')
    return problems


def runValidateManifests(manifestsRoot):
    print('===== 清单结构校验 =====')
    total = 0
    failed = 0
    for versionDir in findVersionDirs(manifestsRoot):
        total += 1
        rel = versionDir.relative_to(manifestsRoot)
        try:
            yamlFiles = sorted(versionDir.glob('*.yaml'))
            merged = mergeManifestData(yamlFiles)
        except YamlSyntaxError as exc:
            failed += 1
            print(f'[FAIL] {rel}: YAML 解析错误 {exc}')
            continue
        problems = checkManifestStructures(merged, str(rel))
        if problems:
            failed += 1
            for p in problems:
                print(f'[FAIL] {p}')
        else:
            print(f'[ OK ] {rel}')
    print(f'\n共 {total} 个版本清单，异常 {failed} 个')
    return 1 if failed else 0


def runCheckIndex(dbPath, pagesDir, canary):
    print('===== 索引一致性校验 =====')
    if not dbPath.is_file():
        print(f'[FAIL] 索引文件不存在: {dbPath}')
        return 1
    con = sqlite3.connect(str(dbPath))
    cur = con.cursor()

    ids = {r[0] for r in cur.execute('SELECT id FROM ids')}
    if canary not in ids:
        print(f'[FAIL] canary 包未进入索引: {canary}（同步可能 missing/no-version 被静默跳过）')
        con.close()
        return 1
    print(f'[ OK ] canary 已入索引: {canary}')

    parts = {rowid: (parent, name)
             for rowid, parent, name in
             cur.execute('SELECT rowid, "parent", pathpart FROM pathparts')}

    def resolve(rowid):
        chain = []
        while rowid in parts and parts[rowid][1] is not None:
            parent, name = parts[rowid]
            chain.append(name)
            rowid = parent
        return '/'.join(reversed(chain))

    rows = cur.execute('SELECT id, pathpart FROM manifest').fetchall()
    idCache = {rowid: v for rowid, v in cur.execute('SELECT rowid, id FROM ids')}
    missing = 0
    for mId, leaf in rows:
        pkgId = idCache.get(mId, '?')
        relPath = resolve(leaf)
        if not (pagesDir / relPath).is_file():
            missing += 1
            print(f'[FAIL] 索引指向的文件缺失: {pkgId} -> {relPath}')
    con.close()
    if missing:
        print(f'\n共有 {missing} 条索引清单在 dist/pages 下找不到对应文件（路径/大小写错位）')
        return 1
    print(f'\n索引共 {len(rows)} 条 manifest，文件全部存在')
    return 0


def main():
    parser = argparse.ArgumentParser(description='离线校验清单与索引')
    parser.add_argument('--manifests', default=str(REPO_ROOT / 'manifests'))
    parser.add_argument('--stage', default=str(REPO_ROOT / 'dist' / 'stage' / 'Public' / 'index.db'))
    parser.add_argument('--pages', default=str(REPO_ROOT / 'dist' / 'pages'))
    parser.add_argument('--canary', default='Obsidian.Obsidian')
    parser.add_argument('--check-index', action='store_true')
    parser.add_argument('--validate-manifests', action='store_true')
    args = parser.parse_args()

    doIndex = args.check_index or not (args.check_index or args.validate_manifests)
    doYaml = args.validate_manifests or not (args.check_index or args.validate_manifests)

    rc = 0
    if doYaml:
        code = runValidateManifests(Path(args.manifests))
        rc = rc or code
    if doIndex:
        code = runCheckIndex(Path(args.stage), Path(args.pages), args.canary)
        rc = rc or code
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
