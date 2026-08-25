#!/usr/bin/env python3
"""将多文件 winget 清单（version + installer + locale）合并为 ManifestType:singleton 单文件。

winget Microsoft.PreIndexed.Package 源的索引 pathpart 仅指向一个文件，
不会自动拉取同目录的 installer/locale 伴随文件。
本脚本将三文件合并为单文件，使 winget 只需拉一个文件即可获得完整清单。

用法:
    python3 scripts/flatten_manifests.py [--input manifests] [--output dist/pages/manifests]

退出码: 0 全部成功；1 有异常；2 参数错误。
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mini_yaml import load as safe_load, YamlSyntaxError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r'^\d+(\.\d+)*([\-+].+)?$')


def flattenVersionDir(versionDir):
    """将一个版本目录内的多文件清单合并为单文件；返回写入的文件路径。"""
    yamlFiles = sorted(versionDir.glob('*.yaml'))
    if not yamlFiles:
        return None, '无 yaml 文件'

    if len(yamlFiles) == 1:
        return yamlFiles[0], None  # 已是单文件

    versionData = installerData = None
    locales = []

    for f in yamlFiles:
        try:
            data = safe_load(f.read_bytes().decode('utf-8-sig')) or {}
        except YamlSyntaxError as e:
            return None, f'YAML 解析错误 {f.name}: {e}'
        mType = data.get('ManifestType', '')
        if mType == 'version':
            versionData = data
        elif mType == 'installer':
            installerData = data
        elif mType in ('locale', 'defaultLocale'):
            locales.append((data, f))

    if not versionData:
        return None, '缺少 version 清单'
    if not installerData:
        return None, '缺少 installer 清单'

    defaultLocale = versionData.get('DefaultLocale', '')
    localeData = None
    for ld, lf in locales:
        if ld.get('PackageLocale', '') == defaultLocale:
            localeData = ld
            break
    if not localeData and locales:
        localeData = locales[0][0]

    if not localeData:
        return None, f'缺少 DefaultLocale={defaultLocale} 对应的 locale 清单'

    merged = {}
    # 以 installer 清单为基底：其顶层 InstallerType/InstallerSwitches/ExpectedReturnCodes/
    # AppsAndFeaturesEntries 等是安装必需字段，仅取 Installers 会全部丢失导致客户端按 unknown 类型安装失败
    merged.update(installerData)
    merged.update(versionData)
    merged.update(localeData)
    merged['Installers'] = installerData.get('Installers', [])
    merged['ManifestType'] = 'singleton'
    versions = [
        versionData.get('ManifestVersion', '0.0.0'),
        installerData.get('ManifestVersion', '0.0.0'),
        localeData.get('ManifestVersion', '0.0.0'),
    ]
    merged['ManifestVersion'] = max(versions)

    outPath = versionDir / (versionData['PackageIdentifier'] + '.yaml')
    outPath.write_text(_dumpYaml(merged), encoding='utf-8')
    for f in yamlFiles:
        if f != outPath:
            f.unlink()
    return outPath, None


def _dumpYaml(data, indent=0):
    """简易 YAML 序列化器，覆盖 winget 清单实际使用的结构。"""
    lines = []
    prefix = '  ' * indent
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list):
                lines.append(f'{prefix}{key}:')
                for item in val:
                    if isinstance(item, dict):
                        first = True
                        for k2, v2 in item.items():
                            if isinstance(v2, dict):
                                lines.append(f'{prefix}    {k2}:')
                                lines.append(_dumpYaml(v2, indent + 2))
                            elif isinstance(v2, list):
                                lines.append(f'{prefix}    {k2}:')
                                lines.append(_dumpYaml(v2, indent + 2))
                            elif isinstance(v2, str) and '\n' in v2:
                                lines.append(f'{prefix}    {k2}: |')
                                for line in v2.split('\n'):
                                    lines.append(f'{prefix}      {line}')
                            else:
                                if first:
                                    lines.append(f'{prefix}  - {k2}: {_scalar(v2)}')
                                    first = False
                                else:
                                    lines.append(f'{prefix}    {k2}: {_scalar(v2)}')
                    else:
                        lines.append(f'{prefix}  - {_scalar(item)}')
            elif isinstance(val, dict):
                lines.append(f'{prefix}{key}:')
                lines.append(_dumpYaml(val, indent + 1))
            elif isinstance(val, str) and '\n' in val:
                lines.append(f'{prefix}{key}: |')
                for line in val.split('\n'):
                    lines.append(f'{prefix}  {line}')
            else:
                lines.append(f'{prefix}{key}: {_scalar(val)}')
    return '\n'.join(lines) + '\n'


def _scalar(val):
    if val is None:
        return ''
    s = str(val)
    if s == '':
        return '""'
    needs_quote = (' ' in s or ':' in s or '#' in s or
                   s.startswith('{') or s.startswith('[') or
                   s.startswith('"') or s.startswith("'") or
                   s.lower() in ('true', 'false', 'null', 'yes', 'no'))
    if needs_quote:
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return s


def main():
    parser = argparse.ArgumentParser(description='多文件清单合并为 singleton')
    parser.add_argument('--input', default=str(REPO_ROOT / 'manifests'))
    parser.add_argument('--output', default=str(REPO_ROOT / 'dist' / 'pages' / 'manifests'))
    args = parser.parse_args()

    inRoot = Path(args.input)
    outRoot = Path(args.output)
    if not inRoot.is_dir():
        print(f'输入目录不存在: {inRoot}')
        return 2

    if outRoot.exists():
        import shutil
        shutil.rmtree(outRoot)
    outRoot.mkdir(parents=True, exist_ok=True)

    total = flattened = copied = skipped = 0
    for versionDir in sorted(inRoot.rglob('*')):
        if not versionDir.is_dir() or not VERSION_RE.match(versionDir.name):
            continue
        if not list(versionDir.glob('*.yaml')):
            continue
        total += 1
        rel = versionDir.relative_to(inRoot)
        dstDir = outRoot / rel
        dstDir.mkdir(parents=True, exist_ok=True)

        for f in versionDir.glob('*.yaml'):
            (dstDir / f.name).write_bytes(f.read_bytes())

        outPath, err = flattenVersionDir(dstDir)
        if err:
            print(f'[FAIL] {rel}: {err}')
            skipped += 1
        elif outPath:
            flattened += 1
            print(f'[ OK ] {rel} (flattened)')
        else:
            copied += 1

    print(f'\n合计 {total} 个版本: 合并 {flattened}, 原样 {copied}, 跳过 {skipped}')
    return 1 if skipped else 0


if __name__ == '__main__':
    raise SystemExit(main())
