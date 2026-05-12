#!/usr/bin/env python3
"""Cocos Creator 项目资源审计脚本。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


TEXT_EXTENSIONS = {
    ".ts",
    ".js",
    ".json",
    ".prefab",
    ".scene",
    ".fire",
    ".anim",
    ".meta",
    ".effect",
    ".mtl",
    ".txt",
    ".md",
}

IGNORED_DIRS = {
    ".git",
    "library",
    "local",
    "temp",
    "tmp",
    "node_modules",
    ".creator",
}

ASSET_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".psd",
    ".mp3",
    ".ogg",
    ".wav",
    ".m4a",
    ".mp4",
    ".json",
    ".plist",
    ".atlas",
    ".skel",
    ".prefab",
    ".scene",
    ".fire",
    ".ttf",
    ".otf",
    ".fnt",
    ".bin",
}


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for filename in filenames:
            yield Path(dirpath) / filename


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def file_hash(path: Path) -> str:
    hasher = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def parse_meta_uuid(meta_path: Path) -> str | None:
    try:
        data = json.loads(read_text(meta_path))
    except json.JSONDecodeError:
        text = read_text(meta_path)
        match = re.search(r'"uuid"\s*:\s*"([^"]+)"', text)
        return match.group(1) if match else None
    uuid = data.get("uuid")
    return uuid if isinstance(uuid, str) else None


def scan_uuid_references(root: Path) -> str:
    chunks: list[str] = []
    for path in iter_files(root):
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if path.name.endswith(".meta"):
            continue
        text = read_text(path)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def count_json_items(path: Path) -> tuple[int, int, int]:
    text = read_text(path)
    if not text:
        return 0, 0, 0
    nodes = text.count('"__type__"') + text.count('"_name"')
    components = text.count('"__id__"') + text.count('"node"')
    deps = text.count('"__uuid__"') + text.count('"uuid"')
    return nodes, components, deps


def build_report(project_root: Path) -> str:
    files = [p for p in iter_files(project_root) if p.is_file()]
    assets_root = project_root / "assets"
    asset_files = [
        p
        for p in files
        if p.suffix.lower() != ".meta"
        and (assets_root in p.parents or p.suffix.lower() in ASSET_EXTENSIONS)
    ]

    total_size = sum(p.stat().st_size for p in asset_files)
    by_ext: Counter[str] = Counter()
    by_dir: Counter[str] = Counter()
    large_files: list[tuple[int, Path]] = []
    hashes: dict[tuple[int, str], list[Path]] = defaultdict(list)

    for path in asset_files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        by_ext[path.suffix.lower() or "(no ext)"] += size
        first_dir = rel(path.parent, project_root).split("/")[0]
        by_dir[first_dir] += size
        large_files.append((size, path))
        if size > 0 and path.suffix.lower() not in {".meta"}:
            hashes[(size, file_hash(path))].append(path)

    duplicates = [
        (paths[0].stat().st_size, paths)
        for paths in hashes.values()
        if len(paths) > 1
    ]
    duplicates.sort(reverse=True, key=lambda item: item[0] * (len(item[1]) - 1))

    uuid_blob = scan_uuid_references(project_root)
    orphan_candidates: list[Path] = []
    meta_count = 0
    for meta in files:
        if not meta.name.endswith(".meta"):
            continue
        target = Path(str(meta)[:-5])
        if not target.exists() or target.suffix.lower() == "":
            continue
        uuid = parse_meta_uuid(meta)
        if not uuid:
            continue
        meta_count += 1
        if uuid not in uuid_blob:
            orphan_candidates.append(target)

    complex_assets = []
    for path in asset_files:
        if path.suffix.lower() not in {".prefab", ".scene", ".fire"}:
            continue
        nodes, components, deps = count_json_items(path)
        score = nodes + components + deps
        if score:
            complex_assets.append((score, nodes, components, deps, path))
    complex_assets.sort(reverse=True)

    lines = [
        "# Cocos Creator 项目资源审计草稿",
        "",
        f"- 项目路径：`{project_root}`",
        f"- 扫描文件数：{len(files)}",
        f"- 资源候选数：{len(asset_files)}",
        f"- 资源候选总体积：{human_size(total_size)}",
        "",
        "## 目录体积 Top 10",
        "",
    ]
    for dirname, size in by_dir.most_common(10):
        lines.append(f"- `{dirname}`：{human_size(size)}")

    lines += ["", "## 扩展名体积 Top 15", ""]
    for ext, size in by_ext.most_common(15):
        lines.append(f"- `{ext}`：{human_size(size)}")

    lines += ["", "## 大文件 Top 30", ""]
    for size, path in sorted(large_files, reverse=True)[:30]:
        lines.append(f"- {human_size(size)} `{rel(path, project_root)}`")

    lines += ["", "## 完全重复文件 Top 20", ""]
    if duplicates:
        for size, paths in duplicates[:20]:
            saved = size * (len(paths) - 1)
            lines.append(f"- 单文件 {human_size(size)}，合并候选可节省约 {human_size(saved)}")
            for path in paths[:8]:
                lines.append(f"  - `{rel(path, project_root)}`")
            if len(paths) > 8:
                lines.append(f"  - 另有 {len(paths) - 8} 个同哈希文件")
    else:
        lines.append("- 未发现完全重复文件。")

    lines += ["", "## 疑似未引用资源 Top 50", ""]
    lines.append(f"> 基于 `.meta` uuid 静态搜索，共检查 {meta_count} 个 uuid。动态字符串加载、远程配置和编辑器插件可能导致误报。")
    lines.append("")
    if orphan_candidates:
        orphan_candidates.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
        for path in orphan_candidates[:50]:
            size = path.stat().st_size if path.exists() else 0
            lines.append(f"- {human_size(size)} `{rel(path, project_root)}`")
    else:
        lines.append("- 未发现明显未引用资源候选。")

    lines += ["", "## 复杂 Scene/Prefab 候选", ""]
    if complex_assets:
        for score, nodes, components, deps, path in complex_assets[:30]:
            lines.append(
                f"- score={score} nodes≈{nodes} components≈{components} deps≈{deps} `{rel(path, project_root)}`"
            )
    else:
        lines.append("- 未发现可解析的 Scene/Prefab 复杂度候选。")

    lines += [
        "",
        "## 下一步人工核查建议",
        "",
        "- 对重复文件：先确认 `.meta` 引用、平台差异和美术命名，再统一引用后删除冗余副本。",
        "- 对疑似未引用资源：在编辑器和运行时日志中确认没有动态加载后再删除。",
        "- 对大图片：核对实际显示尺寸、透明通道、压缩格式和平台纹理压缩配置。",
        "- 对大音频：区分 BGM 与短音效，检查格式、采样率、循环加载和预加载策略。",
        "- 对复杂场景/Prefab：检查首屏依赖、节点层级、对象池、常驻节点和可拆分的懒加载模块。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 Cocos Creator 项目资源体积、重复资源和复杂 Prefab/Scene。")
    parser.add_argument("project_root", type=Path, help="Cocos Creator 项目根目录")
    parser.add_argument("--output", type=Path, help="输出 Markdown 文件；不提供则打印到终端")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    if not project_root.exists():
        raise SystemExit(f"项目路径不存在：{project_root}")

    report = build_report(project_root)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
