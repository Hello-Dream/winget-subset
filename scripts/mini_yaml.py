#!/usr/bin/env python3
"""winget 清单专用的精简 YAML 解析器（零依赖）。

仅支持 winget-pkgs 清单实际使用的语法子集：
    * 块级映射与块级列表（含列表项内嵌映射），缩进以空格计；
    * 标量一律返回字符串，自动去除成对的单/双引号；
    * 行注释（引号外的 " #" 或行首 "#"）。

不支持的语法（流式 {} []、多行块 |、锚点等）会抛出 YamlSyntaxError，
避免静默解析出错误数据。
"""

__all__ = ['load', 'YamlSyntaxError']

import re

# 块标量指示符: |、>、|-、>+、|2 等
_BLOCK_MARKER_RE = re.compile(r'^[|>][0-9+\-]*$')


class YamlSyntaxError(ValueError):
    pass


class _Line:
    __slots__ = ('indent', 'content', 'num')

    def __init__(self, indent, content, num):
        self.indent = indent
        self.content = content
        self.num = num


def _stripComment(text):
    """去掉引号外从 " #" 起的注释部分。"""
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == '#' and (i == 0 or text[i - 1] in (' ', '\t')):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _tokenize(text):
    lines = []
    for num, raw in enumerate(text.splitlines(), 1):
        stripped = _stripComment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(' '))
        if '\t' in stripped[:indent + 1]:
            raise YamlSyntaxError(f'第 {num} 行: 不允许使用 Tab 缩进')
        lines.append(_Line(indent, stripped.strip(), num))
    return lines


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _consumeBlockScalar(lines, pos, keyIndent):
    """吞掉块标量（| 或 >）的全部缩进内容行，返回拼接文本与新位置。"""
    parts = []
    while pos < len(lines) and lines[pos].indent > keyIndent:
        parts.append(lines[pos].content)
        pos += 1
    return '\n'.join(parts), pos


def _splitKeyValue(content):
    """按第一个冒号+空格切分；无值时返回 (key, None)。"""
    idx = -1
    quote = None
    for i, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ':':
            nxt = content[i + 1:i + 2]
            if nxt == '' or nxt == ' ':
                idx = i
                break
    if idx < 0:
        return None
    key = _unquote(content[:idx].strip())
    rest = content[idx + 1:].strip()
    return key, (_unquote(rest) if rest else None)


def _parseBlock(lines, pos, indent):
    """解析同一缩进级别的块；返回 (value, 新位置)。"""
    if pos >= len(lines):
        raise YamlSyntaxError('意外的块结尾')
    isList = lines[pos].content.startswith('- ') or lines[pos].content == '-'
    result = [] if isList else {}

    while pos < len(lines):
        line = lines[pos]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise YamlSyntaxError(f'第 {line.num} 行: 缩进异常')

        if isList:
            if not (line.content == '-' or line.content.startswith('- ')):
                break
            itemText = line.content[1:].strip()
            if not itemText:
                item, pos = _parseChild(lines, pos + 1, line.indent)
                result.append(item)
                continue
            keyValue = _splitKeyValue(itemText)
            if keyValue is None:
                result.append(_unquote(itemText))
                pos += 1
                continue
            item = {}
            key, value = keyValue
            if value is None:
                item[key], pos = _parseChild(lines, pos + 1, line.indent)
            elif _BLOCK_MARKER_RE.match(value):
                item[key], pos = _consumeBlockScalar(lines, pos + 1, line.indent)
            else:
                item[key] = value
                pos += 1
            # 同一列表项的后续键（更深或同级缩进的普通键）
            item, pos = _mergeItemLines(lines, pos, line.indent, item)
            result.append(item)
        else:
            keyValue = _splitKeyValue(line.content)
            if keyValue is None:
                raise YamlSyntaxError(f'第 {line.num} 行: 无法解析映射条目 "{line.content}"')
            key, value = keyValue
            if key in result:
                raise YamlSyntaxError(f'第 {line.num} 行: 键重复 "{key}"')
            if value is None:
                result[key], pos = _parseChild(lines, pos + 1, indent)
            elif _BLOCK_MARKER_RE.match(value):
                result[key], pos = _consumeBlockScalar(lines, pos + 1, indent)
            else:
                result[key] = value
                pos += 1
    return result, pos


def _mergeItemLines(lines, pos, dashIndent, item):
    """吸收列表项中除首键外的其余键；仅限缩进严格大于破折号列的行。"""
    while pos < len(lines) and lines[pos].indent > dashIndent:
        line = lines[pos]
        if line.content.startswith('- ') or line.content == '-':
            break
        keyValue = _splitKeyValue(line.content)
        if keyValue is None:
            raise YamlSyntaxError(f'第 {line.num} 行: 无法解析列表项字段')
        key, value = keyValue
        if value is None:
            item[key], pos = _parseChild(lines, pos + 1, line.indent)
        elif _BLOCK_MARKER_RE.match(value):
            item[key], pos = _consumeBlockScalar(lines, pos + 1, line.indent)
        else:
            item[key] = value
            pos += 1
    return item, pos


def _parseChild(lines, pos, parentIndent):
    """解析键后的子块：允许更深缩进的标准块，也允许与父键同缩进的块级列表。"""
    if pos >= len(lines):
        return None, pos
    nxt = lines[pos]
    deeper = nxt.indent > parentIndent
    sameLevelList = nxt.indent == parentIndent and \
        (nxt.content.startswith('- ') or nxt.content == '-')
    if not (deeper or sameLevelList):
        return None, pos
    return _parseBlock(lines, pos, nxt.indent)


def load(text):
    """解析 YAML 文本并返回 Python 对象。"""
    lines = _tokenize(text)
    if not lines:
        return None
    value, pos = _parseBlock(lines, 0, lines[0].indent)
    if pos != len(lines):
        raise YamlSyntaxError(f'第 {lines[pos].num} 行: 存在未归属的内容')
    return value
