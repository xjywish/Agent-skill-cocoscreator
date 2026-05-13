#!/usr/bin/env python3
"""Audit a Cocos Creator project and render a developer-friendly report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".psd"}
AUDIO_EXTENSIONS = {".mp3", ".ogg", ".wav", ".m4a"}
SCENE_EXTENSIONS = {".prefab", ".scene", ".fire"}


@dataclass
class DuplicateGroup:
    size: int
    saved: int
    paths: list[Path]


@dataclass
class ComplexAsset:
    score: int
    nodes: int
    components: int
    deps: int
    path: Path


@dataclass
class AuditData:
    project_root: Path
    files_count: int
    asset_count: int
    total_size: int
    by_dir: Counter[str]
    by_ext: Counter[str]
    large_files: list[tuple[int, Path]]
    duplicates: list[DuplicateGroup]
    orphan_candidates: list[Path]
    complex_assets: list[ComplexAsset]
    meta_count: int
    resources_size: int
    build_size: int
    image_size: int
    audio_size: int


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


def ratio(part: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{part / total * 100:.1f}%"


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


def classify_priority(data: AuditData) -> tuple[str, list[str]]:
    risks: list[str] = []
    duplicate_saved = sum(group.saved for group in data.duplicates)
    if data.resources_size > 0 and data.total_size > 0 and data.resources_size / data.total_size >= 0.25:
        risks.append("resources 目录占比较高，首包和隐性依赖风险较大。")
    if duplicate_saved >= 5 * 1024 * 1024:
        risks.append(f"完全重复资源可节省约 {human_size(duplicate_saved)}，适合优先治理。")
    if data.orphan_candidates:
        risks.append(f"发现 {len(data.orphan_candidates)} 个疑似未引用资源候选，需要人工确认。")
    if data.complex_assets and data.complex_assets[0].score >= 200:
        risks.append("存在复杂 Scene/Prefab，需关注首屏加载、实例化峰值和节点层级。")
    if data.image_size > data.total_size * 0.5 and data.total_size > 0:
        risks.append("图片资源占比超过 50%，纹理压缩和分辨率收益可能较高。")
    if len(risks) >= 3:
        return "需要重点优化", risks
    if risks:
        return "存在可优化项", risks
    return "未发现明显高风险", risks


def audit_project(project_root: Path) -> AuditData:
    files = [p for p in iter_files(project_root) if p.is_file()]
    assets_root = project_root / "assets"
    asset_files = [
        p
        for p in files
        if p.suffix.lower() != ".meta"
        and (assets_root in p.parents or p.suffix.lower() in ASSET_EXTENSIONS)
    ]

    total_size = 0
    by_ext: Counter[str] = Counter()
    by_dir: Counter[str] = Counter()
    large_files: list[tuple[int, Path]] = []
    hashes: dict[tuple[int, str], list[Path]] = defaultdict(list)
    resources_size = 0
    build_size = 0
    image_size = 0
    audio_size = 0

    for path in asset_files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        suffix = path.suffix.lower()
        path_rel = rel(path, project_root)
        total_size += size
        by_ext[suffix or "(no ext)"] += size
        by_dir[path_rel.split("/")[0]] += size
        if "/resources/" in path_rel or path_rel.startswith("assets/resources/"):
            resources_size += size
        if path_rel.startswith("build/"):
            build_size += size
        if suffix in IMAGE_EXTENSIONS:
            image_size += size
        if suffix in AUDIO_EXTENSIONS:
            audio_size += size
        large_files.append((size, path))
        if size > 0:
            hashes[(size, file_hash(path))].append(path)

    duplicates = [
        DuplicateGroup(size=paths[0].stat().st_size, saved=paths[0].stat().st_size * (len(paths) - 1), paths=paths)
        for paths in hashes.values()
        if len(paths) > 1
    ]
    duplicates.sort(reverse=True, key=lambda group: group.saved)

    uuid_blob = scan_uuid_references(project_root)
    orphan_candidates: list[Path] = []
    meta_count = 0
    for meta in files:
        if not meta.name.endswith(".meta"):
            continue
        target = Path(str(meta)[:-5])
        if not target.exists() or not target.suffix:
            continue
        uuid = parse_meta_uuid(meta)
        if not uuid:
            continue
        meta_count += 1
        if uuid not in uuid_blob:
            orphan_candidates.append(target)
    orphan_candidates.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)

    complex_assets: list[ComplexAsset] = []
    for path in asset_files:
        if path.suffix.lower() not in SCENE_EXTENSIONS:
            continue
        nodes, components, deps = count_json_items(path)
        score = nodes + components + deps
        if score:
            complex_assets.append(ComplexAsset(score, nodes, components, deps, path))
    complex_assets.sort(reverse=True, key=lambda item: item.score)

    return AuditData(
        project_root=project_root,
        files_count=len(files),
        asset_count=len(asset_files),
        total_size=total_size,
        by_dir=by_dir,
        by_ext=by_ext,
        large_files=sorted(large_files, reverse=True),
        duplicates=duplicates,
        orphan_candidates=orphan_candidates,
        complex_assets=complex_assets,
        meta_count=meta_count,
        resources_size=resources_size,
        build_size=build_size,
        image_size=image_size,
        audio_size=audio_size,
    )


def md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(data: AuditData) -> str:
    health, risks = classify_priority(data)
    duplicate_saved = sum(group.saved for group in data.duplicates)
    lines = [
        "# Cocos Creator 游戏项目优化审计报告",
        "",
        "## 0. 结论面板",
        "",
        f"- 项目路径：`{data.project_root}`",
        f"- 健康度：{health}",
        f"- 资源候选总体积：{human_size(data.total_size)}",
        f"- 完全重复资源预计可节省：{human_size(duplicate_saved)}",
        f"- 疑似未引用资源：{len(data.orphan_candidates)} 个",
        f"- 复杂 Scene/Prefab 候选：{len(data.complex_assets)} 个",
        "",
        "## 1. 游戏开发者视角总评",
        "",
    ]
    lines.extend(f"- {item}" for item in risks)
    if not risks:
        lines.append("- 静态扫描未发现明显高风险项，建议继续结合真机性能、构建包和运行日志复核。")

    lines += [
        "",
        "## 2. 数据画像",
        "",
        f"- 扫描文件数：{data.files_count}",
        f"- 资源候选数：{data.asset_count}",
        f"- 图片资源：{human_size(data.image_size)}（{ratio(data.image_size, data.total_size)}）",
        f"- 音频资源：{human_size(data.audio_size)}（{ratio(data.audio_size, data.total_size)}）",
        f"- resources 目录：{human_size(data.resources_size)}（{ratio(data.resources_size, data.total_size)}）",
        "",
        "### 目录体积 Top 10",
        "",
    ]
    lines.extend(md_table(["目录", "体积", "占比"], [[d, human_size(s), ratio(s, data.total_size)] for d, s in data.by_dir.most_common(10)]))
    lines += ["", "### 扩展名体积 Top 15", ""]
    lines.extend(md_table(["扩展名", "体积", "占比"], [[e, human_size(s), ratio(s, data.total_size)] for e, s in data.by_ext.most_common(15)]))
    lines += ["", "### 大文件 Top 30", ""]
    lines.extend(md_table(["体积", "文件"], [[human_size(s), f"`{rel(p, data.project_root)}`"] for s, p in data.large_files[:30]]))

    lines += ["", "## 3. 优先级优化清单", ""]
    if data.resources_size:
        lines += [
            "### P1 检查 resources 目录是否扩大首包和隐性依赖",
            f"- 证据：resources 目录候选体积 {human_size(data.resources_size)}，占资源候选 {ratio(data.resources_size, data.total_size)}。",
            "- 游戏影响：可能增加首包、启动加载和小游戏包体压力。",
            "- 建议改法：把非启动必需资源迁移到明确 Asset Bundle 或远程包，按玩法/活动/皮肤拆分。",
            "- 验证方式：重新构建后对比首包大小、启动耗时和资源加载日志。",
            "",
        ]
    if data.duplicates:
        lines += [
            "### P1 合并完全重复资源",
            f"- 证据：发现 {len(data.duplicates)} 组完全重复文件，理论可节省 {human_size(duplicate_saved)}。",
            "- 游戏影响：减少包体和下载体积，降低资源管理混乱。",
            "- 建议改法：逐组确认 `.meta` 引用和平台差异后统一引用，删除冗余副本。",
            "- 验证方式：编辑器依赖检查、构建后资源缺失检查、关键场景 smoke test。",
            "",
        ]
    if data.orphan_candidates:
        lines += [
            "### P2 清理疑似未引用资源候选",
            f"- 证据：静态 uuid 搜索发现 {len(data.orphan_candidates)} 个候选。",
            "- 游戏影响：可能减少包体和构建时间。",
            "- 建议改法：结合动态加载路径、远程配置和编辑器依赖逐项确认。",
            "- 验证方式：删除前建分支，跑构建、核心流程和资源缺失日志。",
            "",
        ]
    if not (data.resources_size or data.duplicates or data.orphan_candidates):
        lines.append("- 暂无脚本可自动确认的高优先级优化项，请结合运行时性能继续人工审计。")

    lines += ["", "## 4. 专项审计底稿", "", "### 完全重复文件 Top 20", ""]
    if data.duplicates:
        for group in data.duplicates[:20]:
            lines.append(f"- 单文件 {human_size(group.size)}，可节省约 {human_size(group.saved)}")
            for path in group.paths[:8]:
                lines.append(f"  - `{rel(path, data.project_root)}`")
            if len(group.paths) > 8:
                lines.append(f"  - 另有 {len(group.paths) - 8} 个同哈希文件")
    else:
        lines.append("- 未发现完全重复文件。")

    lines += ["", "### 疑似未引用资源 Top 50", ""]
    lines.append(f"> 基于 `.meta` uuid 静态搜索，共检查 {data.meta_count} 个 uuid；动态字符串加载和远程配置可能导致误报。")
    if data.orphan_candidates:
        for path in data.orphan_candidates[:50]:
            size = path.stat().st_size if path.exists() else 0
            lines.append(f"- {human_size(size)} `{rel(path, data.project_root)}`")
    else:
        lines.append("- 未发现明显候选。")

    lines += ["", "### 复杂 Scene/Prefab 候选", ""]
    if data.complex_assets:
        lines.extend(md_table(
            ["评分", "节点≈", "组件≈", "依赖≈", "文件"],
            [[str(a.score), str(a.nodes), str(a.components), str(a.deps), f"`{rel(a.path, data.project_root)}`"] for a in data.complex_assets[:30]],
        ))
    else:
        lines.append("- 未发现可解析的 Scene/Prefab 复杂度候选。")

    lines += [
        "",
        "## 5. 需要人工确认",
        "",
        "- 动态字符串加载、远程配置、热更新清单是否引用了候选资源。",
        "- 平台差异资源是否不能合并或删除。",
        "- 大 Scene/Prefab 是否处于首屏或核心战斗链路。",
        "- 对象池、释放策略和事件监听需要结合代码与真机 Profiler 验证。",
    ]
    return "\n".join(lines) + "\n"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def progress_bar(percent: str) -> str:
    try:
        width = max(0.0, min(float(percent.rstrip("%")), 100.0))
    except ValueError:
        width = 0.0
    return f'<span class="bar"><span style="width:{width:.1f}%"></span></span>'


def render_rows(rows: list[list[str]]) -> str:
    return "\n".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)


def render_html(data: AuditData) -> str:
    health, risks = classify_priority(data)
    duplicate_saved = sum(group.saved for group in data.duplicates)
    top_dir_rows = [
        [f"<code>{esc(name)}</code>", esc(human_size(size)), progress_bar(ratio(size, data.total_size)), esc(ratio(size, data.total_size))]
        for name, size in data.by_dir.most_common(10)
    ]
    top_ext_rows = [
        [f"<code>{esc(name)}</code>", esc(human_size(size)), progress_bar(ratio(size, data.total_size)), esc(ratio(size, data.total_size))]
        for name, size in data.by_ext.most_common(15)
    ]
    large_rows = [
        [esc(human_size(size)), f"<code>{esc(rel(path, data.project_root))}</code>"]
        for size, path in data.large_files[:30]
    ]
    duplicate_cards = []
    for group in data.duplicates[:12]:
        items = "".join(f"<li><code>{esc(rel(path, data.project_root))}</code></li>" for path in group.paths[:6])
        more = f"<li>另有 {len(group.paths) - 6} 个同哈希文件</li>" if len(group.paths) > 6 else ""
        duplicate_cards.append(
            f"""
            <article class="issue-card">
              <div><span class="badge p1">P1</span><strong>重复资源组，可节省 {esc(human_size(group.saved))}</strong></div>
              <p>单文件 {esc(human_size(group.size))}，合并前确认 .meta、Prefab/Scene 引用和平台差异。</p>
              <ul>{items}{more}</ul>
            </article>
            """
        )
    orphan_rows = [
        [esc(human_size(path.stat().st_size if path.exists() else 0)), f"<code>{esc(rel(path, data.project_root))}</code>"]
        for path in data.orphan_candidates[:50]
    ]
    complex_rows = [
        [esc(asset.score), esc(asset.nodes), esc(asset.components), esc(asset.deps), f"<code>{esc(rel(asset.path, data.project_root))}</code>"]
        for asset in data.complex_assets[:30]
    ]
    risk_items = "".join(f"<li>{esc(item)}</li>" for item in risks)
    if not risk_items:
        risk_items = "<li>静态扫描未发现明显高风险项，建议结合真机 Profiler 和构建包继续复核。</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cocos Creator 游戏项目优化审计报告</title>
  <style>
    :root {{
      --ink: #1f2933;
      --muted: #667085;
      --line: #d9e2ec;
      --panel: #ffffff;
      --wash: #f6f8fb;
      --teal: #0f8b8d;
      --coral: #d95d39;
      --gold: #b7791f;
      --green: #2f855a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: var(--wash);
      line-height: 1.6;
    }}
    header {{
      padding: 40px 48px 28px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }}
    header h1 {{
      margin: 0 0 8px;
      font-size: 30px;
      letter-spacing: 0;
    }}
    header p {{ margin: 0; color: var(--muted); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 24px 56px; }}
    section {{ margin: 0 0 28px; }}
    h2 {{ font-size: 22px; margin: 0 0 14px; }}
    code {{
      font-family: "Cascadia Mono", Consolas, monospace;
      background: #eef3f8;
      border: 1px solid #d8e3ee;
      border-radius: 5px;
      padding: 1px 5px;
      word-break: break-all;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }}
    .card, .issue-card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .metric .label {{ color: var(--muted); font-size: 13px; }}
    .metric .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .metric.teal {{ border-top: 4px solid var(--teal); }}
    .metric.coral {{ border-top: 4px solid var(--coral); }}
    .metric.gold {{ border-top: 4px solid var(--gold); }}
    .metric.green {{ border-top: 4px solid var(--green); }}
    .risk-list {{ margin: 0; padding-left: 20px; }}
    .issues {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .issue-card p {{ margin: 8px 0; color: var(--muted); }}
    .issue-card ul {{ margin: 8px 0 0; padding-left: 20px; }}
    .badge {{
      display: inline-block;
      min-width: 36px;
      text-align: center;
      margin-right: 8px;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      color: #fff;
    }}
    .p1 {{ background: var(--gold); }}
    .p2 {{ background: var(--teal); }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ background: #eef3f8; font-size: 13px; color: #344054; }}
    tr:last-child td {{ border-bottom: 0; }}
    .bar {{
      display: inline-block;
      width: 140px;
      height: 9px;
      background: #e6edf3;
      border-radius: 999px;
      overflow: hidden;
      vertical-align: middle;
    }}
    .bar span {{ display: block; height: 100%; background: var(--teal); }}
    .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
    .note {{ color: var(--muted); font-size: 14px; margin-top: 8px; }}
    @media (max-width: 820px) {{
      header {{ padding: 28px 22px 20px; }}
      main {{ padding: 20px 14px 40px; }}
      .grid, .issues, .two-col {{ grid-template-columns: 1fr; }}
      table {{ font-size: 14px; }}
      th, td {{ padding: 8px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Cocos Creator 游戏项目优化审计报告</h1>
    <p>项目路径：<code>{esc(data.project_root)}</code></p>
  </header>
  <main>
    <section class="grid">
      <div class="card metric teal"><div class="label">健康度</div><div class="value">{esc(health)}</div></div>
      <div class="card metric coral"><div class="label">资源候选总体积</div><div class="value">{esc(human_size(data.total_size))}</div></div>
      <div class="card metric gold"><div class="label">重复资源可节省</div><div class="value">{esc(human_size(duplicate_saved))}</div></div>
      <div class="card metric green"><div class="label">疑似未引用资源</div><div class="value">{len(data.orphan_candidates)} 个</div></div>
    </section>
    <section class="panel">
      <h2>1. 游戏开发者视角总评</h2>
      <ul class="risk-list">{risk_items}</ul>
      <p class="note">静态扫描用于定位高收益线索；删除资源、拆 Bundle、改释放策略前仍需结合编辑器依赖、构建包、运行日志和真机 Profiler 验证。</p>
    </section>
    <section>
      <h2>2. 数据画像</h2>
      <div class="grid">
        <div class="card metric"><div class="label">扫描文件数</div><div class="value">{data.files_count}</div></div>
        <div class="card metric"><div class="label">资源候选数</div><div class="value">{data.asset_count}</div></div>
        <div class="card metric"><div class="label">图片资源</div><div class="value">{esc(human_size(data.image_size))}</div><div class="label">{esc(ratio(data.image_size, data.total_size))}</div></div>
        <div class="card metric"><div class="label">音频资源</div><div class="value">{esc(human_size(data.audio_size))}</div><div class="label">{esc(ratio(data.audio_size, data.total_size))}</div></div>
      </div>
    </section>
    <section>
      <h2>3. 优先级优化清单</h2>
      <div class="issues">
        <article class="issue-card"><div><span class="badge p1">P1</span><strong>检查 resources 目录与首包压力</strong></div><p>resources 候选体积 {esc(human_size(data.resources_size))}，占比 {esc(ratio(data.resources_size, data.total_size))}。非启动必需资源建议迁移到 Asset Bundle 或远程包。</p></article>
        <article class="issue-card"><div><span class="badge p1">P1</span><strong>合并完全重复资源</strong></div><p>发现 {len(data.duplicates)} 组完全重复资源，理论可节省 {esc(human_size(duplicate_saved))}。合并前必须确认 .meta 引用和平台差异。</p></article>
        <article class="issue-card"><div><span class="badge p2">P2</span><strong>清理疑似未引用资源候选</strong></div><p>发现 {len(data.orphan_candidates)} 个候选。动态加载、远程配置、热更新清单可能造成误报，不能直接删除。</p></article>
        <article class="issue-card"><div><span class="badge p2">P2</span><strong>复核复杂 Scene/Prefab</strong></div><p>发现 {len(data.complex_assets)} 个复杂候选。优先检查首屏、大厅、战斗、活动页，关注实例化峰值、节点层级和依赖加载。</p></article>
      </div>
    </section>
    <section class="two-col">
      <div><h2>4. 目录体积 Top 10</h2><table><thead><tr><th>目录</th><th>体积</th><th>占比</th><th></th></tr></thead><tbody>{render_rows(top_dir_rows)}</tbody></table></div>
      <div><h2>5. 扩展名体积 Top 15</h2><table><thead><tr><th>类型</th><th>体积</th><th>占比</th><th></th></tr></thead><tbody>{render_rows(top_ext_rows)}</tbody></table></div>
    </section>
    <section><h2>6. 大文件 Top 30</h2><table><thead><tr><th>体积</th><th>文件</th></tr></thead><tbody>{render_rows(large_rows) or '<tr><td colspan="2">无数据</td></tr>'}</tbody></table></section>
    <section><h2>7. 重复资源</h2><div class="issues">{''.join(duplicate_cards) or '<article class="issue-card">未发现完全重复文件。</article>'}</div></section>
    <section><h2>8. 疑似未引用资源 Top 50</h2><p class="note">基于 .meta uuid 静态搜索，共检查 {data.meta_count} 个 uuid。动态字符串加载、远程配置和编辑器插件可能导致误报。</p><table><thead><tr><th>体积</th><th>文件</th></tr></thead><tbody>{render_rows(orphan_rows) or '<tr><td colspan="2">未发现明显候选</td></tr>'}</tbody></table></section>
    <section><h2>9. 复杂 Scene/Prefab 候选</h2><table><thead><tr><th>评分</th><th>节点≈</th><th>组件≈</th><th>依赖≈</th><th>文件</th></tr></thead><tbody>{render_rows(complex_rows) or '<tr><td colspan="5">未发现可解析候选</td></tr>'}</tbody></table></section>
    <section class="panel"><h2>10. 下一步人工确认</h2><ul><li>核查动态加载路径、远程配置、热更新清单是否引用候选资源。</li><li>用编辑器依赖检查确认重复资源是否能统一引用。</li><li>结合真机 Profiler 复核大 Scene/Prefab 的加载峰值、节点数、DrawCall 和内存释放。</li><li>重新构建后对比首包大小、启动耗时、核心场景切换耗时和资源缺失日志。</li></ul></section>
  </main>
</body>
</html>
"""


def choose_format(output: Path | None, requested: str) -> str:
    if requested != "auto":
        return requested
    if output and output.suffix.lower() in {".html", ".htm"}:
        return "html"
    return "md"


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 Cocos Creator 项目并输出游戏开发者视角的优化报告。")
    parser.add_argument("project_root", type=Path, help="Cocos Creator 项目根目录")
    parser.add_argument("--output", type=Path, help="输出文件；.html 自动输出 HTML，其他默认 Markdown")
    parser.add_argument("--format", choices=["auto", "html", "md"], default="auto", help="报告格式，默认根据输出扩展名判断")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    if not project_root.exists():
        raise SystemExit(f"项目路径不存在：{project_root}")

    data = audit_project(project_root)
    report_format = choose_format(args.output, args.format)
    report = render_html(data) if report_format == "html" else render_markdown(data)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
