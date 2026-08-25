#!/usr/bin/env python3
"""遍历 manifests/ 子集，构建 winget PreIndexed 源的 index.db（schema v1.6）。

表结构移植自 cloudflightio/winget-pkgs（Apache-2.0，见 NOTICE.md），SQL 全部参数绑定。
用法: python3 scripts/build_index.py --manifests manifests --output dist/stage/Public/index.db
"""

import argparse
import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mini_yaml import load as safe_load

# 非 UTF-8 控制台（如 GitHub windows runner 的 cp1252）打印中文会崩溃，统一转 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and getattr(_stream, 'reconfigure', None):
        _stream.reconfigure(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION_RE = re.compile(r'^\d+(\.\d+)*([\-+].+)?$')

# schema v1.6 —— 与 winget 客户端 PreIndexed 源读取逻辑保持兼容
SCHEMA_SQL = """
BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS "metadata" (
    "name"  TEXT NOT NULL,
    "value" TEXT NOT NULL,
    PRIMARY KEY("name")
);
CREATE TABLE IF NOT EXISTS "ids" (
    "rowid" INTEGER,
    "id"    TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "names" (
    "rowid" INTEGER,
    "name"  TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "monikers" (
    "rowid"   INTEGER,
    "moniker" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "versions" (
    "rowid"   INTEGER,
    "version" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "channels" (
    "rowid"   INTEGER,
    "channel" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "pathparts" (
    "rowid"     INTEGER,
    "parent"    INT64,
    "pathpart"  TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "manifest" (
    "rowid"           INTEGER,
    "id"              INT64 NOT NULL,
    "name"            INT64 NOT NULL,
    "moniker"         INT64 NOT NULL,
    "version"         INT64 NOT NULL,
    "channel"         INT64 NOT NULL,
    "pathpart"        INT64 NOT NULL,
    "arp_min_version" INT64,
    "arp_max_version" INT64,
    "hash"            BLOB,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "tags" (
    "rowid" INTEGER,
    "tag"   TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "tags_map" (
    "manifest" INT64 NOT NULL,
    "tag"      INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "commands" (
    "rowid"   INTEGER,
    "command" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "commands_map" (
    "manifest" INT64 NOT NULL,
    "command"  INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "upgradecodes" (
    "rowid"       INTEGER,
    "upgradecode" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "upgradecodes_map" (
    "manifest"    INT64 NOT NULL,
    "upgradecode" INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "pfns" (
    "rowid" INTEGER,
    "pfn"   TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "pfns_map" (
    "manifest" INT64 NOT NULL,
    "pfn"      INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "productcodes" (
    "rowid"       INTEGER,
    "productcode" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "productcodes_map" (
    "manifest"    INT64 NOT NULL,
    "productcode" INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "norm_names" (
    "rowid"     INTEGER,
    "norm_name" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "norm_names_map" (
    "manifest"  INT64 NOT NULL,
    "norm_name" INT64 NOT NULL
);
CREATE TABLE IF NOT EXISTS "norm_publishers" (
    "rowid"          INTEGER,
    "norm_publisher" TEXT NOT NULL,
    PRIMARY KEY("rowid")
);
CREATE TABLE IF NOT EXISTS "norm_publishers_map" (
    "manifest"       INT64 NOT NULL,
    "norm_publisher" INT64 NOT NULL
);
CREATE INDEX IF NOT EXISTS "manifest_id_index" ON "manifest" ("id");
CREATE INDEX IF NOT EXISTS "manifest_name_index" ON "manifest" ("name");
CREATE INDEX IF NOT EXISTS "manifest_moniker_index" ON "manifest" ("moniker");
CREATE UNIQUE INDEX IF NOT EXISTS "tags_map_pkindex" ON "tags_map" ("tag", "manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "commands_map_pkindex" ON "commands_map" ("command", "manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "upgradecodes_pkindex" ON "upgradecodes" ("upgradecode");
CREATE UNIQUE INDEX IF NOT EXISTS "upgradecodes_map_pkindex" ON "upgradecodes_map" ("upgradecode", "manifest");
CREATE INDEX IF NOT EXISTS "upgradecodes_map_index" ON "upgradecodes_map" ("manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "pfns_pkindex" ON "pfns" ("pfn");
CREATE UNIQUE INDEX IF NOT EXISTS "pfns_map_pkindex" ON "pfns_map" ("pfn", "manifest");
CREATE INDEX IF NOT EXISTS "pfns_map_index" ON "pfns_map" ("manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "productcodes_pkindex" ON "productcodes" ("productcode");
CREATE UNIQUE INDEX IF NOT EXISTS "productcodes_map_pkindex" ON "productcodes_map" ("productcode", "manifest");
CREATE INDEX IF NOT EXISTS "productcodes_map_index" ON "productcodes_map" ("manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "norm_names_pkindex" ON "norm_names" ("norm_name");
CREATE UNIQUE INDEX IF NOT EXISTS "norm_names_map_pkindex" ON "norm_names_map" ("norm_name", "manifest");
CREATE INDEX IF NOT EXISTS "norm_names_map_index" ON "norm_names_map" ("manifest");
CREATE UNIQUE INDEX IF NOT EXISTS "norm_publishers_pkindex" ON "norm_publishers" ("norm_publisher");
CREATE UNIQUE INDEX IF NOT EXISTS "norm_publishers_map_pkindex" ON "norm_publishers_map" ("norm_publisher", "manifest");
CREATE INDEX IF NOT EXISTS "norm_publishers_map_index" ON "norm_publishers_map" ("manifest");
COMMIT;
"""


class IndexBuilder:
    """封装 index.db 的行级写入；内部维护各字符串表的 rowid 复用。"""

    def __init__(self, dbPath):
        dbPath.parent.mkdir(parents=True, exist_ok=True)
        if dbPath.exists():
            dbPath.unlink()
        self.con = sqlite3.connect(dbPath)
        # 一次性重建场景，关掉日志与刷盘换取写入速度
        self.con.execute('PRAGMA journal_mode=OFF')
        self.con.execute('PRAGMA synchronous=OFF')
        self.cur = self.con.cursor()
        self.cur.executescript(SCHEMA_SQL)
        # 固定行: versions/channels 的 rowid=1 为空串；pathparts 根节点固定 rowid=1
        self.cur.execute('INSERT INTO versions (rowid, version) VALUES (?, ?)', (1, ''))
        self.cur.execute('INSERT INTO channels (rowid, channel) VALUES (?, ?)', (1, ''))
        self.cur.execute(
            'INSERT INTO metadata (name, value) VALUES (?, ?), (?, ?), (?, ?)',
            ('majorVersion', '1', 'minorVersion', '6', 'lastwritetime', str(int(time.time()))),
        )
        self.cur.execute('INSERT INTO pathparts (rowid, parent, pathpart) VALUES (?, NULL, ?)', (1, 'manifests'))
        self.pathpartCache = {('manifests',): 1}
        self.idCache = {}
        self.monikerSentinelId = None
        self.manifestRowId = 0

    def getId(self, table, field, value):
        key = (table, value)
        cached = self.idCache.get(key)
        if cached:
            return cached
        row = self.cur.execute(f'SELECT rowid FROM {table} WHERE {field} = ?', (value,)).fetchone()
        if row:
            rowId = row[0]
        else:
            rowId = self.cur.execute(f'SELECT MAX(rowid) + 1 FROM {table}').fetchone()[0] or 1
            self.cur.execute(f'INSERT INTO {table} (rowid, {field}) VALUES (?, ?)', (rowId, value))
        self.idCache[key] = rowId
        return rowId

    def getPathpartId(self, parts):
        """按完整路径取/建 pathparts 节点 id。

        节点唯一性由 (parent, pathpart) 决定而非仅名字 —— 否则形如
        n/Notepad++/Notepad++ 的同名父子段会复用同一行并形成环。
        """
        if parts in self.pathpartCache:
            return self.pathpartCache[parts]
        parentId = self.getPathpartId(parts[:-1])
        row = self.cur.execute(
            'SELECT rowid FROM pathparts WHERE pathpart = ? AND "parent" = ?',
            (parts[-1], parentId),
        ).fetchone()
        if row:
            rowId = row[0]
        else:
            self.cur.execute('SELECT MAX(rowid) + 1 FROM pathparts')
            rowId = self.cur.fetchone()[0] or 1
            self.cur.execute(
                'INSERT INTO pathparts (rowid, "parent", pathpart) VALUES (?, ?, ?)',
                (rowId, parentId, parts[-1]),
            )
        self.pathpartCache[parts] = rowId
        return rowId

    def getMoniker(self, moniker):
        # 无 Moniker 时复用共享空串节点，避免伪造可搜索关键词
        if moniker is None:
            if self.monikerSentinelId is None:
                self.monikerSentinelId = self.getId('monikers', 'moniker', '')
            return self.monikerSentinelId
        return self.getId('monikers', 'moniker', moniker)

    @staticmethod
    def normalize(name):
        return name.replace(' ', '').lower()

    def addManifest(self, merged, relParts, versionYamlBytes):
        """写入一个版本目录对应的 manifest 行及其关联数据。"""
        pkgId = merged['PackageIdentifier']
        self.manifestRowId += 1
        rowId = self.manifestRowId

        idCol = self.getId('ids', 'id', pkgId)
        packageName = merged.get('PackageName') or pkgId.split('.')[-1]
        nameCol = self.getId('names', 'name', packageName)
        monikerCol = self.getMoniker(merged.get('Moniker'))
        versionCol = self.getId('versions', 'version', merged['PackageVersion'])
        leafPathId = self.getPathpartId(tuple(relParts))
        fileHash = hashlib.sha256(versionYamlBytes).digest()

        self.cur.execute(
            '''INSERT INTO manifest
               (rowid, id, name, moniker, version, channel, pathpart,
                arp_min_version, arp_max_version, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (rowId, idCol, nameCol, monikerCol, versionCol, 1, leafPathId, 1, 1, fileHash),
        )
        self.cur.execute(
            'INSERT INTO norm_names_map (manifest, norm_name) VALUES (?, ?)',
            (rowId, self.getId('norm_names', 'norm_name', self.normalize(packageName))),
        )

        publisher = merged.get('Publisher') or pkgId.split('.')[0]
        self.cur.execute(
            'INSERT INTO norm_publishers_map (manifest, norm_publisher) VALUES (?, ?)',
            (rowId, self.getId('norm_publishers', 'norm_publisher', self.normalize(publisher))),
        )

        for tag in merged.get('Tags') or []:
            self.cur.execute(
                'INSERT OR IGNORE INTO tags_map (manifest, tag) VALUES (?, ?)',
                (rowId, self.getId('tags', 'tag', tag)),
            )
        for command in merged.get('Commands') or []:
            self.cur.execute(
                'INSERT OR IGNORE INTO commands_map (manifest, command) VALUES (?, ?)',
                (rowId, self.getId('commands', 'command', command)),
            )

        # 遍历全部 installer 条目；ProductCode/UpgradeCode 还可能位于
        # AppsAndFeaturesEntries（ARP 条目）中，需一并收集，否则客户端安装校验失败
        for inst in merged.get('Installers') or []:
            if inst.get('PackageFamilyName'):
                self.cur.execute(
                    'INSERT OR IGNORE INTO pfns_map (manifest, pfn) VALUES (?, ?)',
                    (rowId, self.getId('pfns', 'pfn', inst['PackageFamilyName'])),
                )
            for entry in [inst] + list(inst.get('AppsAndFeaturesEntries') or []):
                if entry.get('ProductCode'):
                    self.cur.execute(
                        'INSERT OR IGNORE INTO productcodes_map (manifest, productcode) VALUES (?, ?)',
                        (rowId, self.getId('productcodes', 'productcode', entry['ProductCode'])),
                    )
                if entry.get('UpgradeCode'):
                    self.cur.execute(
                        'INSERT OR IGNORE INTO upgradecodes_map (manifest, upgradecode) VALUES (?, ?)',
                        (rowId, self.getId('upgradecodes', 'upgradecode', entry['UpgradeCode'])),
                    )

    def finish(self):
        self.cur.execute('UPDATE metadata SET value=? WHERE name=?', (str(int(time.time())), 'lastwritetime'))
        self.con.commit()
        self.con.close()


def collectManifestYamls(versionDir):
    return sorted(versionDir.glob('*.yaml'))


def mergeManifestData(yamlFiles):
    # 后读优先，仅保留构建索引需要的键
    merged = {}
    for f in yamlFiles:
        data = safe_load(f.read_bytes().decode('utf-8-sig')) or {}
        for key in ('PackageIdentifier', 'PackageVersion', 'PackageName', 'Publisher',
                    'Moniker', 'Tags', 'Commands', 'Installers'):
            if key in data:
                merged[key] = data[key]
    required = ('PackageIdentifier', 'PackageVersion')
    missing = [k for k in required if k not in merged]
    if missing:
        raise ValueError(f'缺少必需字段: {missing}')
    return merged


def findVersionDirs(manifestsRoot):
    result = []
    for dirPath in manifestsRoot.rglob('*'):
        if dirPath.is_dir() and VERSION_RE.match(dirPath.name) \
                and list(dirPath.glob('*.yaml')):
            result.append(dirPath.relative_to(manifestsRoot))
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description='构建 winget 源 index.db')
    parser.add_argument('--manifests', default=str(REPO_ROOT / 'manifests'))
    parser.add_argument('--output', default=str(REPO_ROOT / 'dist/stage/Public/index.db'))
    args = parser.parse_args()

    manifestsRoot = Path(args.manifests)
    output = Path(args.output)
    if not manifestsRoot.is_dir():
        print(f'清单目录不存在: {manifestsRoot}')
        return 1

    builder = IndexBuilder(output)
    count = 0
    try:
        for relDir in findVersionDirs(manifestsRoot):
            versionDir = manifestsRoot / relDir
            yamlFiles = collectManifestYamls(versionDir)
            try:
                merged = mergeManifestData(yamlFiles)
            except Exception as exc:
                print(f'[跳过] {relDir}: {exc}')
                continue

            # 叶节点固定为版本清单 <Id>.yaml（哈希亦取自该文件）
            pkgYamlName = merged['PackageIdentifier'] + '.yaml'
            versionYaml = next(
                (f for f in yamlFiles if f.name == pkgYamlName), None)
            if versionYaml is None:
                versionYaml = next(
                    f for f in yamlFiles
                    if '.installer.' not in f.name and '.locale.' not in f.name)
            hashBytes = versionYaml.read_bytes()

            # 完整相对路径含版本目录: manifests/<首字母>/.../<版本>/<Id>.yaml
            relParts = ['manifests'] + list(relDir.parts) + [versionYaml.name]
            builder.addManifest(merged, relParts, hashBytes)
            count += 1
    finally:
        builder.finish()

    print(f'index.db 构建完成: {output} （共 {count} 个 manifest）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
