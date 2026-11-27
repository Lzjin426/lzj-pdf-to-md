#!/usr/bin/env python3
"""Markdown 转换结果的机械检查：纯子图标签、计数快照、前后对比。

子命令：
  scan            扫描文件/目录，输出计数快照与问题清单（只读）
  fix-sublabels   删除整行只有 (a)/(b)/(c) 的标签行（默认 dry-run，--write 才落盘）
  verify          对比 scan 生成的快照，计数变化或纯标签残留即失败

只用标准库，Markdown 通用，不依赖具体阅读平台。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata

IMAGE_TAG = re.compile(r"<img\b", re.I)
IMAGE_MD = re.compile(r"!\[[^\]]*\]\(")
CAPTION_ATTR = re.compile(r"data-quiet-caption=")
FIGCAPTION_TAG = re.compile(r"<figcaption\b", re.I)
TABLE_TAG = re.compile(r"<table\b", re.I)
ROW_TAG = re.compile(r"<tr\b", re.I)
CELL_TAG = re.compile(r"<t[dh]\b", re.I)
HEADING_MD = re.compile(r"^#{1,6}\s")
HEADING_RICH = re.compile(r'data-quiet-block="heading"')
TAG_CMD = re.compile(r"\\tag\b")
PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")

EMPHASIS = re.compile(r"[*_`~]+")
LIST_MARK = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
HTML_WRAP = re.compile(r"</?(?:p|br|span|div|center)\b[^>]*>", re.I)
PURE_LABEL = re.compile(r"^[（(]\s*([a-hA-H])\s*[)）]$")
LABEL_PREFIX = re.compile(r"^[（(]\s*([a-hA-H])\s*[)）](.*)$")

FIGURE_START = re.compile(r"^(?:FIGURE|Figure|图)\s*[0-9A-Za-z]")
IMAGE_HINT = re.compile(r"(<img\b|data-quiet-caption=|!\[[^\]]*\]\()", re.I)
PROSE_CUE = re.compile(
    r"(示出|显示|给出|表明|说明|表示|示意|所示|示明|可知|比较|列出|绘制"
    r"|shows|show|indicates|gives|illustrates|depicts)",
    re.I,
)
FIGURE_IN_PROSE = re.compile(r"^(?:图|FIGURE|Figure)\s*[0-9A-Za-z.\-]+\s*中")
ARTIFACTS = [
    ("¼", re.compile(r"¼")),
    ("908", re.compile(r"(?<!\d)908(?!\d)")),
    ("1808", re.compile(r"(?<!\d)1808(?!\d)")),
    ("contact angle a", re.compile(r"contact\s+angle\s+a\b", re.I)),
]


def normalize_label(line: str) -> str:
    """去掉列表符号、强调符、行内 HTML 后返回候选标签行。"""
    s = line.strip()
    s = HTML_WRAP.sub("", s).strip()
    s = LIST_MARK.sub("", s).strip()
    s = EMPHASIS.sub("", s).strip()
    s = unicodedata.normalize("NFKC", s)
    return s


def pure_label_letter(line: str):
    """整行只有 (a)/(b)… 时返回字母，否则 None。"""
    m = PURE_LABEL.match(normalize_label(line))
    return m.group(1).lower() if m else None


def label_prefix_letter(line: str):
    """行首是 (a)/(b)… 时返回字母，否则 None（带文字或不带都算）。"""
    m = LABEL_PREFIX.match(normalize_label(line))
    return m.group(1).lower() if m else None


def iter_md(targets):
    for t in targets:
        if os.path.isdir(t):
            for root, dirs, files in os.walk(t):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in sorted(files):
                    if f.lower().endswith(".md"):
                        yield os.path.join(root, f)
        elif os.path.isfile(t):
            yield t
        else:
            print(f"skip, not found: {t}", file=sys.stderr)


def iter_tables(lines):
    """返回每张 HTML 表的 (rows, cols)，用于结构对比。"""
    out = []
    i = 0
    while i < len(lines):
        if TABLE_TAG.search(lines[i]):
            depth = 0
            rows = 0
            max_cols = 0
            cur_cols = 0
            while i < len(lines):
                seg = lines[i]
                depth += len(re.findall(r"<table\b", seg, re.I))
                depth -= len(re.findall(r"</table\b", seg, re.I))
                rows += len(ROW_TAG.findall(seg))
                cur_cols = len(CELL_TAG.findall(seg))
                max_cols = max(max_cols, cur_cols)
                rows_here = ROW_TAG.findall(seg)
                if rows_here:
                    parts = re.split(r"<tr\b[^>]*>", seg, flags=re.I)[1:]
                    for p in parts:
                        max_cols = max(max_cols, len(CELL_TAG.findall(p)))
                i += 1
                if depth <= 0:
                    break
            out.append([rows, max_cols])
        else:
            i += 1
    return out


def scan_file(path: str) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.split("\n")

    headings = sum(1 for l in lines if HEADING_MD.match(l)) + len(HEADING_RICH.findall(text))
    images = len(IMAGE_TAG.findall(text)) + len(IMAGE_MD.findall(text))
    captions = len(CAPTION_ATTR.findall(text)) + len(FIGCAPTION_TAG.findall(text))
    tables = iter_tables(lines)
    pipe_rows = sum(1 for l in lines if PIPE_ROW.match(l))
    math_tags = len(TAG_CMD.findall(text))

    sublabels = []
    caption_candidates = []
    label_runs = []
    current_run = []

    prev_ne = [None] * len(lines)
    last = None
    for i in range(len(lines)):
        prev_ne[i] = last
        if lines[i].strip():
            last = i
    next_ne = [None] * len(lines)
    last = None
    for i in range(len(lines) - 1, -1, -1):
        next_ne[i] = last
        if lines[i].strip():
            last = i

    for i, line in enumerate(lines):
        idx = i + 1
        letter = pure_label_letter(line)
        prefix = label_prefix_letter(line)
        if letter:
            sublabels.append(idx)
        if prefix:
            current_run.append((idx, prefix))
        elif current_run and line.strip():
            label_runs.append(current_run)
            current_run = []

        stripped = line.strip()
        if FIGURE_START.match(stripped) and len(stripped) < 120:
            context = [stripped]
            for j in (prev_ne[i], next_ne[i]):
                if j is not None:
                    context.append(lines[j])
            attached = any(IMAGE_HINT.search(x) for x in context)
            prose = bool(PROSE_CUE.search(stripped) or FIGURE_IN_PROSE.match(stripped))
            caption_candidates.append(
                {
                    "line": idx,
                    "text": stripped[:100],
                    "attached": attached,
                    "prose": prose,
                    "suspected": not attached and not prose,
                }
            )
    if current_run:
        label_runs.append(current_run)

    order_warnings = []
    for run in label_runs:
        letters = [c[1] for c in run]
        if len(letters) < 2:
            continue
        if len(set(letters)) != len(letters) or letters != sorted(letters):
            order_warnings.append(
                {
                    "lines": [c[0] for c in run],
                    "letters": letters,
                }
            )

    artifacts = {}
    for name, pattern in ARTIFACTS:
        hits = [i for i, l in enumerate(lines, start=1) if pattern.search(l)]
        if hits:
            artifacts[name] = hits

    double_tags = []
    for idx, line in enumerate(lines, start=1):
        if len(TAG_CMD.findall(line)) > 1:
            double_tags.append(idx)

    delim = [i for i, l in enumerate(lines) if l.strip().startswith("$$")]
    formula_warnings = {
        "delims": len(delim),
        "unbalanced": len(delim) % 2 == 1,
        "gap_after_open": [],
        "double_close": [],
        "list_risk": [],
    }
    for pos, i in enumerate(delim):
        stripped = lines[i].strip()
        if stripped == "$$":
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt[:2] in ("- ", "+ ") or re.match(r"\d{1,2}[.)]\s+(?![0-9])", nxt):
                formula_warnings["list_risk"].append(i + 1)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j > i + 1 and j < len(lines) and lines[j].strip() and not lines[j].strip().startswith("$$"):
                body = lines[j].strip()
                if body[:1] in "+-−":
                    formula_warnings["gap_after_open"].append(i + 1)
        elif "tag" in stripped and pos + 1 < len(delim) and delim[pos + 1] - i <= 1:
            formula_warnings["double_close"].append(i + 1)

    return {
        "counts": {
            "headings": headings,
            "images": images,
            "captions": captions,
            "tables": len(tables),
            "table_grids": tables,
            "pipe_rows": pipe_rows,
            "math_tags": math_tags,
            "formula_delims": len(delim),
        },
        "sublabels": sublabels,
        "caption_candidates": caption_candidates,
        "label_order_warnings": order_warnings,
        "artifacts": artifacts,
        "double_tag_lines": double_tags,
        "formula_warnings": formula_warnings,
    }


def cmd_scan(args) -> int:
    result = {}
    total_sublabels = 0
    caption_suspects = 0
    order_warns = 0
    formula_files = 0
    for path in iter_md(args.targets):
        info = scan_file(path)
        result[os.path.abspath(path)] = info
        total_sublabels += len(info["sublabels"])
        caption_suspects += sum(1 for c in info["caption_candidates"] if c["suspected"])
        order_warns += len(info["label_order_warnings"])
        name = os.path.relpath(path, args.root) if args.root else path

        if info["sublabels"] or args.verbose:
            print(f"{name}: 纯子图标签 {len(info['sublabels'])} 行 {info['sublabels'][:20]}")
        for cand in info["caption_candidates"]:
            if cand["suspected"] or args.verbose:
                tag = "图注候选" if cand["suspected"] else ("已挂图" if cand["attached"] else "正文引用")
                print(f"  [{tag}] {name}:{cand['line']} {cand['text']}")
        for warn in info["label_order_warnings"]:
            print(f"  [标签顺序] {name}: 行 {warn['lines']} -> {warn['letters']}")
        fw = info["formula_warnings"]
        has_problem = (
            fw["unbalanced"] or fw["gap_after_open"] or fw["double_close"] or fw["list_risk"]
        )
        if has_problem:
            formula_files += 1
        if has_problem or args.verbose:
            detail = f"  [公式] {name}: 定界符 {fw['delims']} 行"
            if fw["unbalanced"]:
                detail += "，奇数不成对"
            if fw["gap_after_open"]:
                detail += f"，开 $$ 后空行 {fw['gap_after_open'][:10]}"
            if fw["double_close"]:
                detail += f"，多余收尾 $$ {fw['double_close'][:10]}"
            if fw["list_risk"]:
                detail += f"，公式体以 -/+/序号开头会被当列表 {fw['list_risk'][:10]}"
            print(detail)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "files": result}, fh, ensure_ascii=False, indent=1)

    print(
        f"\n扫描 {len(result)} 个文件 | 纯子图标签 {total_sublabels} 行 | "
        f"未挂图的图注候选 {caption_suspects} 条 | 标签顺序告警 {order_warns} 处 | "
        f"公式异常文件 {formula_files} 个"
        + (f" | 快照 {args.json}" if args.json else "")
    )
    return 0


def cmd_fix_sublabels(args) -> int:
    changed = 0
    removed_total = 0
    for path in iter_md(args.targets):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.split("\n")
        removed = []
        kept = []
        for idx, line in enumerate(lines, start=1):
            if pure_label_letter(line):
                removed.append(idx)
                continue
            kept.append(line)
        if not removed:
            continue
        changed += 1
        removed_total += len(removed)
        print(f"{path}: 删除 {len(removed)} 行 -> {removed}")
        if not args.write:
            continue
        if args.backup:
            dest = os.path.join(args.backup, os.path.relpath(os.path.abspath(path), args.root or os.getcwd()))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(path, dest)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(newline.join(kept))
    mode = "已写入" if args.write else "dry-run（加 --write 落盘）"
    print(f"\n{mode} | 涉及 {changed} 个文件 | 共删除 {removed_total} 行")
    return 0


CAP_TAG = re.compile(r'data-quiet-caption="([^"]+)"')
FIGCAP = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.S)
CAP_START = re.compile(r"^(?:图|FIGURE|Figure)\s*[0-9A-Za-z]+")
END_PUNCT = set("。．.；;！!？?，,、：:）)】」”\"'")
CONNECTORS = {
    "即", "或", "故", "于是", "而", "从而", "则", "若", "因", "且", "及", "得",
    "代入", "解得", "故得", "故有", "所以", "因此", "其中", "由此", "由", "设", "取",
    "当", "解", "如图", "见图", "式中", "例", "题",
}


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _flex_head(head: str):
    return re.compile(r"\s*".join(re.escape(ch) for ch in head))


def _is_caption_tail(line: str) -> bool:
    s = line.strip()
    if not 3 <= len(s) <= 14:
        return False
    if s in CONNECTORS:
        return False
    if s[0] in "0123456789（(§第*-":
        return False
    if s[-1] in END_PUNCT:
        return False
    if any(ch in s for ch in "，,、；;：:"):
        return False
    if s.startswith(("图", "FIGURE", "Figure")):
        return False
    if s.startswith(("思考题", "练习题", "习题", "小结", "本章", "参考文献", "附录", "目录", "前言")):
        return False
    return any("一" <= ch <= "鿿" for ch in s)


def _already_in_caption(tail: str, cap: str) -> bool:
    """候选续行里一半以上的汉字已经在图注里，说明是 OCR 换行重复，不算截断。"""
    cap_chars = set(_norm_text(cap))
    chars = [c for c in tail if "一" <= c <= "鿿"]
    if not chars:
        return True
    return sum(1 for c in chars if c in cap_chars) / len(chars) >= 0.5


def _page_lines(doc, idx, page_w):
    lines = []
    for blk in doc[idx].get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            txt = "".join(sp["text"] for sp in ln["spans"]).strip()
            if not txt:
                continue
            x0, _, x1, _ = ln["bbox"]
            lines.append((txt, x0, x1, (x0 + x1) / 2))
    return lines


def _line_of(lines, needle):
    for txt, x0, x1, mid in lines:
        if needle and needle in txt:
            return (txt, x0, x1, mid)
    return None


def cmd_caption_check(args) -> int:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("caption-check 需要 PyMuPDF：pip install pymupdf", file=sys.stderr)
        return 2
    with open(args.md, encoding="utf-8", errors="replace") as fh:
        md = fh.read()
    caps = CAP_TAG.findall(md)
    caps += [re.sub(r"<[^>]+>", "", x).strip() for x in FIGCAP.findall(md)]
    caps = [c.strip() for c in caps if CAP_START.match(c.strip())]
    doc = fitz.open(args.pdf)
    raw_pages = [doc[i].get_text() for i in range(doc.page_count)]
    norm_pages = [_norm_text(t) for t in raw_pages]
    trunc, missing, ok = [], [], 0
    line_cache = {}
    for cap in caps:
        key = _norm_text(cap)
        if len(key) < 4:
            continue
        head = key[:8]
        page_idx = None
        for i, page in enumerate(norm_pages):
            if head in page:
                page_idx = i
                break
        if page_idx is None:
            missing.append({"caption": cap, "page": None, "suggest": ""})
            continue
        raw = raw_pages[page_idx]
        m = _flex_head(key).search(raw) or _flex_head(head).search(raw)
        if not m:
            missing.append({"caption": cap, "page": page_idx + 1, "suggest": ""})
            continue
        tail_of_line = raw[m.end():].split("\n")[0].strip()
        suggest = ""
        if not tail_of_line:
            following = [l.strip() for l in raw[m.end():].split("\n")[1:8] if l.strip()]
            for line in following[:4]:
                if _is_caption_tail(line):
                    suggest = line
                    break
                if len(line) > 14:
                    break
        if suggest:
            page_w = doc[page_idx].rect.width
            if page_idx not in line_cache:
                line_cache[page_idx] = _page_lines(doc, page_idx, page_w)
            lines = line_cache[page_idx]
            cap_line = _line_of(lines, head[:6])
            cand_line = _line_of(lines, suggest[:6])
            if cap_line and cand_line:
                left_margin = page_w * 0.12
                if cand_line[1] < left_margin or abs(cand_line[3] - cap_line[3]) > page_w * 0.28:
                    suggest = ""
        if suggest and not _already_in_caption(suggest, cap):
            trunc.append({"caption": cap, "page": page_idx + 1, "suggest": suggest})
        else:
            ok += 1
    for item in trunc[: args.max_report]:
        print(f"[疑似截断] p{item['page']} 「{item['caption']}」 → PDF 后接「{item['suggest']}」")
    for item in missing[: args.max_report]:
        where = f"p{item['page']}" if item["page"] else "未定位"
        print(f"[未定位] {where} 「{item['caption']}」")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"captions": len(caps), "truncated": trunc, "missing": missing}, fh, ensure_ascii=False, indent=1)
    print(
        f"\n图注核对 {len(caps)} 条 | 完整 {ok} | 疑似截断 {len(trunc)} | 未定位 {len(missing)}"
        + (f" | 明细 {args.json}" if args.json else "")
    )
    return 1 if (args.strict and trunc) else 0


def cmd_verify(args) -> int:
    with open(args.before, encoding="utf-8") as fh:
        before = json.load(fh)["files"]
    problems = []
    checked = 0
    for path in iter_md(args.targets):
        key = os.path.abspath(path)
        if key not in before:
            problems.append(f"快照里没有这个文件：{path}")
            continue
        checked += 1
        now = scan_file(path)["counts"]
        old = before[key]["counts"]
        for field, value in now.items():
            if field == "pipe_rows":
                continue
            if old.get(field) != value:
                problems.append(f"{path}: {field} 从 {old.get(field)} 变为 {value}")
        sublabels = scan_file(path)["sublabels"]
        if sublabels:
            problems.append(f"{path}: 仍有纯子图标签 {len(sublabels)} 行 {sublabels[:20]}")
    if checked == 0:
        problems.append("没有任何文件被比对，检查路径参数")
    if problems:
        print("验收未通过：")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"验收通过：{checked} 个文件计数与表格结构一致，纯子图标签为 0")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown 转换结果的机械检查")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描并输出计数快照与问题清单")
    p_scan.add_argument("targets", nargs="+", help="Markdown 文件或目录")
    p_scan.add_argument("--json", help="把计数快照写到这个文件，供 verify 使用")
    p_scan.add_argument("--root", help="相对路径显示基准，建议填库根")
    p_scan.add_argument("--verbose", action="store_true", help="也列出正常项和全部图注候选")
    p_scan.set_defaults(func=cmd_scan)

    p_fix = sub.add_parser("fix-sublabels", help="删除整行只有 (a)/(b)… 的标签行")
    p_fix.add_argument("targets", nargs="+")
    p_fix.add_argument("--write", action="store_true", help="真正落盘；默认只报告")
    p_fix.add_argument("--backup", help="落盘前的备份根目录")
    p_fix.add_argument("--root", help="配合 --backup 计算相对路径")
    p_fix.set_defaults(func=cmd_fix_sublabels)

    p_verify = sub.add_parser("verify", help="对比扫描快照")
    p_verify.add_argument("targets", nargs="+")
    p_verify.add_argument("--before", required=True, help="scan 生成的快照 JSON")
    p_verify.set_defaults(func=cmd_verify)

    p_cap = sub.add_parser("caption-check", help="用 PDF 文字层核对图注有没有被截断")
    p_cap.add_argument("--md", required=True, help="Markdown 文件")
    p_cap.add_argument("--pdf", required=True, help="原始 PDF")
    p_cap.add_argument("--json", help="把明细写到这个文件")
    p_cap.add_argument("--max-report", type=int, default=50, help="最多打印多少条")
    p_cap.add_argument("--strict", action="store_true", help="发现疑似截断就以非零退出")
    p_cap.set_defaults(func=cmd_caption_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
