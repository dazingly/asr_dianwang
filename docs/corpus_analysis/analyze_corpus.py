#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Operation-ticket corpus read-only analysis.

Usage:
    python -I analyze_corpus.py <corpus_dir> --out-dir <dir>

The script only reads JSON files under <corpus_dir> and writes a Markdown
report plus a JSON stats file under <out-dir>. It does not modify the corpus.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

# Physical device suffixes. Longest match wins.
DEVICE_SUFFIXES = {
    "跳闸出口压板": "device",
    "重合闸出口压板": "device",
    "保护出口压板": "device",
    "出口压板": "device",
    "接地刀闸": "device",
    "接地开关": "device",
    "隔离开关": "device",
    "隔离刀闸": "device",
    "空气开关": "device",
    "负荷开关": "device",
    "电压互感器": "device",
    "电流互感器": "device",
    "消弧线圈": "device",
    "熔断器": "device",
    "站用变": "device",
    "所用变": "device",
    "软压板": "device",
    "硬压板": "device",
    "保护压板": "device",
    "功能压板": "device",
    "断路器": "device",
    "变压器": "device",
    "电容器": "device",
    "电抗器": "device",
    "互感器": "device",
    "避雷器": "device",
    "刀闸": "device",
    "闸刀": "device",
    "地刀": "device",
    "开关": "device",
    "母线": "device",
    "主变": "device",
    "压板": "device",
    "手车": "device",
    "母排": "device",
    "接地线": "device",
    "电缆": "device",
    "线路": "device",
    "导线": "device",
    "引线": "device",
    "套管": "device",
    "触头": "device",
    "静触头": "device",
    "动触头": "device",
    "把手": "device",
    "转换开关": "device",
    "控制开关": "device",
    "按钮": "device",
    "保护装置": "device",
    "测控装置": "device",
    "自动装置": "device",
    "装置": "device",
    "备自投": "device",
    "pt": "device",
    # Panels / places
    "保护屏": "place",
    "测控屏": "place",
    "汇控柜": "place",
    "开关柜": "place",
    "端子箱": "place",
    "机构箱": "place",
    "后台机": "place",
    "顺控主机": "place",
    "监控主机": "place",
}
SUFFIXES_SORTED = sorted(DEVICE_SUFFIXES, key=len, reverse=True)

ACTION_VERBS = [
    ("拉开", "exec"), ("合上", "exec"), ("停用", "exec"), ("投入", "exec"),
    ("拆除", "exec"), ("装设", "exec"), ("切换", "exec"), ("切至", "exec"),
    ("送电", "exec"), ("停电", "exec"), ("充电", "exec"), ("放电", "exec"),
    ("试送", "exec"), ("隔离", "exec"), ("恢复", "exec"), ("加装", "exec"),
    ("移除", "exec"), ("开启", "exec"), ("关闭", "exec"), ("取下", "exec"),
    ("放上", "exec"), ("改为", "exec"), ("改至", "exec"), ("转", "exec"),
    ("推至", "exec"), ("拉至", "exec"), ("摇至", "exec"),
    ("推入", "exec"), ("拉出", "exec"), ("摇入", "exec"), ("摇出", "exec"),
    ("装上", "exec"), ("插入", "exec"), ("拔出", "exec"),
    ("选择", "exec"), ("新建", "exec"), ("预演", "exec"), ("执行", "exec"),
    ("检查", "verify"), ("验明", "verify"), ("核对", "verify"),
    ("确认", "verify"), ("查明", "verify"),
]
ACTION_SORTED = sorted(ACTION_VERBS, key=lambda x: len(x[0]), reverse=True)

STATE_WORDS = [
    "开关及线路检修", "线路检修", "开关检修",
    "热备用", "冷备用", "运行", "检修", "备用", "充电", "放电",
    "试验位置", "运行位置", "检修位置", "工作位置",
    "分闸位置", "合闸位置", "远方", "就地",
    "并列", "解列", "分列", "合环", "解环", "确环",
    "无电压", "有电压", "确已拉开", "确已合上",
    "确在分闸位置", "确在合闸位置", "确无电压",
    "正常", "正确", "符合", "具备", "条件",
]
STATE_SORTED = sorted(STATE_WORDS, key=len, reverse=True)

STATE_FINAL_SORTED = [
    "开关及线路检修", "线路检修", "开关检修",
    "试验位置", "运行位置", "检修位置", "工作位置",
    "热备用", "冷备用", "分闸位置", "合闸位置",
    "分列", "并列", "解列",
    "检修", "运行", "备用", "充电", "放电", "远方", "就地",
]

OBJECT_WORDS = [
    "二次电压回路", "电压回路", "控制回路", "合闸回路", "跳闸回路",
    "电气位置指示", "机械位置指示", "位置指示",
    "低频低压减载", "低周减载", "低频减载",
    "跳闸出口", "重合闸出口", "出口压板", "软压板", "硬压板", "压板",
    "保护", "重合闸", "跳闸", "合闸", "分闸", "信号", "出口",
    "把手", "手车", "母排", "接地线", "备自投", "自动装置",
]
OBJECT_SORTED = sorted(OBJECT_WORDS, key=len, reverse=True)

PLACE_WORDS = [
    "测控屏", "汇控柜", "开关柜", "保护屏", "后台机", "顺控主机",
    "监控主机", "端子箱", "机构箱", "主控室", "高压室", "配电室",
    "电缆室", "母线室", "现场", "屏内", "柜内", "屏柜", "送电区域",
]
PLACE_SORTED = sorted(PLACE_WORDS, key=len, reverse=True)

SCOPE_WORDS = ["系统", "全站", "站内"]
SCOPE_SORTED = sorted(SCOPE_WORDS, key=len, reverse=True)

RELATION_WORDS = [
    "切至", "改为", "改至", "之间", "由", "转", "至", "到",
    "在", "和", "及", "与", "或", "并", "后",
]
PUNCT = set("，。；、：,.;:!?！？")

SUSPECT_TOKENS = {
    "方武": "方式（OCR/录入）",
    "护手": "把手（OCR/录入）",
    "场至": "切至（OCR/录入）",
    "地手电": "把手由（OCR/录入）",
    "分留": "分裂（OCR/录入）",
    "分刘": "分裂（OCR/录入）",
    "检对": "核对（OCR/录入）",
    "炫对": "核对（OCR/录入）",
    "低颜": "低频（OCR/录入）",
    "顺环": "合环/环网（待确认）",
    "转档": "转带（OCR/录入）",
    "确环": "合环（待确认）",
}

VOLT_RE = re.compile(r"(\d{1,4})\s*(kv|千伏)")
VOLT_SPLIT_RE = re.compile(r"(\d{1,4})\s*(kv|千伏)(?!\s)")
ID_RE = re.compile(r"[a-z0-9#]+(?:-[a-z0-9#]+)*")
ROMAN_RE = re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹ]")
GLUED_TOKEN_RE = re.compile(r"[a-z0-9#-]+kv")
STANDARD_VOLTAGES = ["1000", "750", "500", "330", "220", "110", "66", "35", "20", "10", "6"]
PLATE_HEADS = {
    "压板", "软压板", "硬压板", "出口压板", "跳闸出口压板",
    "重合闸出口压板", "保护压板", "功能压板", "把手",
}
# "场至" is an OCR/typo variant of "切至"; it is kept here so that the
# underlying transition can still be counted, while SUSPECT_TOKENS flags it.
TRANSITION_MARKERS = [
    "切至", "推至", "拉至", "摇至", "推入", "拉出", "摇入", "摇出",
    "改为", "改至", "场至", "转",
]
TRANS_PATTERN = re.compile(
    r"由(.{0,20}?)(?:" + "|".join(TRANSITION_MARKERS) + r")(.{0,20}?)("
    + "|".join(STATE_FINAL_SORTED)
    + r")"
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def halfwidth(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def clean_text(text: str) -> str:
    text = halfwidth(text)
    text = text.replace(" ", " ")
    return text.strip().lower()


def norm_entry(text: str) -> str:
    return re.sub(r"\s+", "", clean_text(text))


def split_voltage(text: str) -> str:
    """Insert a space after voltage units so 10kv0112 becomes 10kv 0112."""
    return VOLT_SPLIT_RE.sub(lambda m: m.group(1) + "kv ", text)


def glued_voltage_info(text: str):
    """Detect IDs glued to a voltage, e.g. 1CLP235kV -> 1CLP2 + 35kV."""
    results = []
    for m in GLUED_TOKEN_RE.finditer(text):
        token = m.group()
        if not re.search(r"[a-z]", token):
            continue
        body = token[:-2]
        candidates = []
        for voltage in STANDARD_VOLTAGES:
            if body.endswith(voltage):
                prefix = body[: -len(voltage)]
                if prefix and re.search(r"[a-z]", prefix):
                    candidates.append((prefix, voltage))
        if not candidates:
            continue
        candidates.sort(key=lambda item: len(item[1]), reverse=True)
        prefix, voltage = candidates[0]
        results.append({
            "token": token,
            "prefix": prefix,
            "voltage": voltage + "kv",
            "ambiguous": len(candidates) > 1,
            "candidates": candidates,
        })
    return results


def _split_glued_match(match):
    token = match.group()
    if not re.search(r"[a-z]", token):
        return token
    body = token[:-2]
    candidates = []
    for voltage in STANDARD_VOLTAGES:
        if body.endswith(voltage):
            prefix = body[: -len(voltage)]
            if prefix and re.search(r"[a-z]", prefix):
                candidates.append((prefix, voltage))
    if not candidates:
        return token
    candidates.sort(key=lambda item: len(item[1]), reverse=True)
    prefix, voltage = candidates[0]
    return prefix + " " + voltage + "kv"


def split_glued_voltage(text: str) -> str:
    return GLUED_TOKEN_RE.sub(_split_glued_match, text)


def short(text: str, limit: int = 78) -> str:
    text = text.replace("|", "／").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_terms(text: str, terms) -> list:
    """Longest-match, non-overlapping term scan (avoids 热备用 -> 备用)."""
    ordered = sorted(terms, key=len, reverse=True)
    found = []
    i = 0
    n = len(text)
    while i < n:
        for term in ordered:
            if text.startswith(term, i):
                found.append(term)
                i += len(term)
                break
        else:
            i += 1
    return found


def mask_spans(text: str, spans) -> str:
    chars = list(text)
    for s, e, _, _ in spans:
        for i in range(s, e):
            chars[i] = " "
    return "".join(chars)


# ---------------------------------------------------------------------------
# Device phrase parsing
# ---------------------------------------------------------------------------

def find_suffix_spans(text: str):
    spans = []
    i = 0
    n = len(text)
    while i < n:
        for suffix in SUFFIXES_SORTED:
            if text.startswith(suffix, i):
                spans.append((i, i + len(suffix), suffix, DEVICE_SUFFIXES[suffix]))
                i += len(suffix)
                break
        else:
            i += 1
    return spans


def find_delimiter_spans(text: str, suffix_spans):
    raw = []
    for i, ch in enumerate(text):
        if ch in PUNCT:
            raw.append((i, i + 1, ch))
    for word in RELATION_WORDS:
        start = 0
        while True:
            p = text.find(word, start)
            if p < 0:
                break
            inside = any(s <= p < e for s, e, _, _ in suffix_spans)
            if not inside:
                raw.append((p, p + len(word), word))
            start = p + 1
    raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    merged = []
    for span in raw:
        s, e, word = span
        if merged and s < merged[-1][1]:
            if (e - s) > (merged[-1][1] - merged[-1][0]):
                merged[-1] = span
            continue
        merged.append(span)
    return merged


def split_phrases(text: str, delimiters):
    phrases = []
    prev = 0
    for s, e, _ in delimiters:
        if s > prev:
            phrases.append(text[prev:s])
        prev = max(prev, e)
    if prev < len(text):
        phrases.append(text[prev:])
    return phrases


def split_id_token(start: int, token: str):
    """Split an exact '#2pt' / '2pt' into the numeric ID; 'pt' is a suffix.

    '#2PTPL12' must stay whole: it is a disconnector ID, not '#2 + PT'.
    """
    m = re.fullmatch(r"(#\d+)(pt)", token)
    if m:
        return [(start, start + len(m.group(1)), m.group(1))]
    m = re.fullmatch(r"(\d+)(pt)", token)
    if m:
        return [(start, start + len(m.group(1)), m.group(1))]
    return [(start, start + len(token), token)]


def find_ids(text: str):
    text = split_voltage(split_glued_voltage(text))
    ids = []
    for m in ID_RE.finditer(text):
        start, end = m.start(), m.end()
        token = m.group()
        if not re.search(r"\d", token):
            continue
        if token.endswith("kv") or token in {"kv", "v", "a", "mw", "kw", "kva"}:
            continue
        # Ratings / decimals: 0.2级, 3P级 are not device IDs.
        if text[end:end + 1] == "级":
            continue
        if text[start - 1:start] in {".", "．"}:
            continue
        if text[end:end + 1] in {".", "．"} and text[end + 1:end + 2].isdigit():
            continue
        ids.extend(split_id_token(start, token))
    return ids


def id_shape(token: str) -> str:
    if token.startswith("#"):
        return "hash"
    has_alpha = bool(re.search(r"[a-z]", token))
    has_hyphen = "-" in token
    if has_alpha and has_hyphen:
        return "alnum_hyphen"
    if has_alpha:
        return "alnum"
    if has_hyphen:
        return "digit_hyphen"
    return "digit"


def classify_gap(gap: str) -> str:
    if not gap:
        return "none"
    if re.search(r"保护|跳闸|合闸|重合闸|出口|电压|回路|信号|控制|方式|中性点|侧", gap):
        return "function"
    if re.search(r"线|母线|分段|主变|变|站", gap):
        return "name"
    return "other"


def strip_prefix(prefix: str) -> str:
    text = prefix
    text = VOLT_RE.sub("", text)
    for word in PLACE_SORTED:
        text = text.replace(word, "")
    for word, _ in ACTION_SORTED:
        text = text.replace(word, "")
    text = text.strip(" 　将把对在由的为和及与或并")
    text = re.sub(r"[，。；、：,.;:!?！？（）()“”\"']", "", text)
    return text


def is_pure_state_phrase(phrase: str, suffix_spans) -> bool:
    if not any(word in phrase for word in STATE_WORDS):
        return False
    remainder = phrase
    for _, _, suffix, _ in suffix_spans:
        remainder = remainder.replace(suffix, "", 1)
    for word in STATE_SORTED:
        remainder = remainder.replace(word, "")
    for word in ["及", "和", "与", "或", "并", "、", "，", "。", "；", "：", "的", " ", "　"]:
        remainder = remainder.replace(word, "")
    return len(remainder) == 0


def analyze_phrase(phrase: str):
    phrase = phrase.strip(" 　，。；、：,.;:!?！？（）()“”\"'")
    if not phrase:
        return None
    phrase = split_voltage(split_glued_voltage(phrase))
    suffix_spans = find_suffix_spans(phrase)
    if not suffix_spans:
        return None
    head_start, head_end, head, head_cat = suffix_spans[-1]
    # 母线保护 / 主变保护: the head is a protected object, not the device itself.
    if head_cat == "device" and head in {"母线", "主变"}:
        if phrase[head_end:].startswith("保护"):
            head_cat = "object"
    ids = find_ids(phrase)
    ids_before = [x for x in ids if x[0] < head_start]
    # Protection-plate entries often look like 1CLP2 35kV ... 300开关压板:
    # the plate ID is the ID immediately BEFORE the voltage, not the nearest
    # controlled-device ID (300).
    pre_voltage_ids = []
    for item in ids_before:
        rest = phrase[item[1]:]
        if re.match(r"\s*\d{1,4}kv", rest):
            pre_voltage_ids.append(item)
    id_start = id_end = id_token = None
    if pre_voltage_ids and head in PLATE_HEADS:
        id_start, id_end, id_token = max(pre_voltage_ids, key=lambda x: x[1])
    elif ids_before:
        id_start, id_end, id_token = max(ids_before, key=lambda x: x[1])
    ref_start = id_start if id_start is not None else head_start
    prefix = strip_prefix(phrase[:ref_start])
    gap = phrase[id_end:head_start] if id_token is not None else ""
    return {
        "phrase": phrase,
        "head": head,
        "head_cat": head_cat,
        "id": id_token,
        "id_shape": id_shape(id_token) if id_token else "",
        "prefix": prefix,
        "gap": gap,
        "gap_type": classify_gap(gap),
        "suffixes": [s for _, _, s, _ in suffix_spans],
        "is_pair": id_token is not None,
        "pure_state": is_pure_state_phrase(phrase, suffix_spans),
        "pre_voltage_id": id_token if pre_voltage_ids and head in PLATE_HEADS else "",
    }


def analyze_entry(text: str) -> dict:
    raw = text.strip()
    norm = norm_entry(raw)
    text_l = clean_text(raw)
    glued = glued_voltage_info(text_l)
    text_v = split_voltage(split_glued_voltage(text_l))

    suffix_spans = find_suffix_spans(text_v)
    delimiters = find_delimiter_spans(text_v, suffix_spans)
    phrases = split_phrases(text_v, delimiters)
    parsed = [p for p in (analyze_phrase(x) for x in phrases) if p]

    pairs = [p for p in parsed if p["is_pair"]]
    named_devices = []
    for idx, p in enumerate(parsed):
        if p["is_pair"] or p["head_cat"] != "device" or p["pure_state"]:
            continue
        # "开关及线路检修": the bare "开关" before a pure-state phrase is part
        # of the state phrase, not a separate named device.
        nxt = parsed[idx + 1] if idx + 1 < len(parsed) else None
        if nxt and nxt["pure_state"] and len(p["phrase"]) <= 8:
            continue
        named_devices.append(p)
    named_objects = [
        p for p in parsed
        if not p["is_pair"] and p["head_cat"] == "object" and not p["pure_state"]
    ]

    masked = mask_spans(text_v, suffix_spans)
    actions = []
    for word, category in ACTION_SORTED:
        start = 0
        while True:
            pos = masked.find(word, start)
            if pos < 0:
                break
            actions.append((pos, word, category))
            start = pos + len(word)
    actions.sort()

    transitions = []
    for m in TRANS_PATTERN.finditer(text_v):
        transitions.append({
            "from_raw": m.group(1),
            "to_raw": m.group(2),
            "to_state": m.group(3),
        })

    first_action = actions[0] if actions else None
    if transitions:
        family = "状态转换"
    elif first_action and first_action[2] == "verify":
        family = "检查确认"
    elif first_action and first_action[2] == "exec":
        family = "执行操作"
    elif any(w in text_v for w in ["检查", "验明", "核对", "确认"]):
        family = "检查确认"
    elif any(w in text_v for w in ["拉开", "合上", "停用", "投入", "拆除", "装设", "切换"]):
        family = "执行操作"
    else:
        family = "其他"

    states = find_terms(text_v, STATE_WORDS)
    objects = find_terms(text_v, OBJECT_WORDS)
    places = find_terms(text_v, PLACE_WORDS)
    scopes = find_terms(text_v, SCOPE_WORDS)

    has_action = bool(actions) or bool(transitions)
    has_target = bool(pairs or named_devices or named_objects or scopes)
    kir_core = has_action and has_target
    # Some items are place/condition checks (检查现场具备操作条件); the place is
    # the target of the check even though there is no device.
    kir_core_place = has_action and (has_target or bool(places))
    kir_detail = kir_core and bool(states or places)

    voltage_match = VOLT_RE.findall(text_v)
    voltage = voltage_match[-1][0] + "kv" if voltage_match else ""
    voltages = [num + "kv" for num, _ in voltage_match]

    return {
        "raw": raw,
        "norm": norm,
        "family": family,
        "first_action": first_action[1] if first_action else "",
        "has_action": has_action,
        "actions": [a[1] for a in actions],
        "transitions": transitions,
        "states": states,
        "objects": objects,
        "places": places,
        "scopes": scopes,
        "pairs": pairs,
        "named_devices": named_devices,
        "named_objects": named_objects,
        "has_pair": bool(pairs),
        "has_named_device": bool(named_devices),
        "has_named_object": bool(named_objects),
        "has_target": has_target,
        "kir_core": kir_core,
        "kir_core_place": kir_core_place,
        "kir_detail": kir_detail,
        "voltage": voltage,
        "voltages": voltages,
        "glued": glued,
        "glued_count": len(glued),
        "glued_ambiguous": sum(1 for item in glued if item["ambiguous"]),
        "multi_target": (len(pairs) + len(named_devices) + len(named_objects)) >= 2,
        "suspicious": [t for t in SUSPECT_TOKENS if t in text_v],
        "zero_kv": bool(re.search(r"(?<!\d)0kv", text_v)),
        "quote_imbalance": text_v.count("“") != text_v.count("”"),
        "has_roman": bool(ROMAN_RE.search(text_v)),
        "has_colon": "：" in text_v or ":" in text_v,
        "has_relation_range": ("至" in text_v and "之间" in text_v),
        "has_panel_inside": any(w in text_v for w in ["柜内", "屏内", "母排上", "手车内"]),
        "length": len(norm),
    }


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def read_ticket(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, f"{path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name}: root is not an object"
    substation = str(data.get("substation", "")).strip()
    mission = str(data.get("mission", "")).strip()
    entries = data.get("entries", {})
    items = []
    if isinstance(entries, dict):
        def sort_key(kv):
            key = kv[0]
            try:
                return (0, int(key), "")
            except Exception:
                return (1, 0, str(key))
        for key, value in sorted(entries.items(), key=sort_key):
            items.append((str(key), str(value).strip()))
    elif isinstance(entries, list):
        for idx, value in enumerate(entries, 1):
            items.append((str(idx), str(value).strip()))
    else:
        return None, f"{path.name}: entries is not a dict/list"
    return {
        "path": str(path),
        "substation": substation,
        "mission": mission,
        "entries": items,
    }, None


def load_corpus(root: Path):
    files = sorted(root.rglob("*.json"))
    tickets = []
    errors = []
    for path in files:
        ticket, error = read_ticket(path)
        if error:
            errors.append(error)
        elif ticket:
            tickets.append(ticket)
    return files, tickets, errors


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "0.0%"


def counter_table(counter: Counter, total: int, top: int = 20):
    lines = ["| 项 | 次数 | 占唯一条目 |", "|---|---:|---:|"]
    for key, count in counter.most_common(top):
        lines.append(f"| `{short(str(key), 40)}` | {count} | {pct(count, total)} |")
    return "\n".join(lines)


def examples(records, predicate, limit=12):
    lines = []
    seen = set()
    for rec in records:
        if not predicate(rec):
            continue
        key = rec["norm"]
        if key in seen:
            continue
        seen.add(key)
        station = rec.get("stations", "")
        lines.append(f"- `{short(rec['raw'])}` ｜ {station}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else "（无）"


def build_report(root: Path, files, tickets, errors) -> tuple[str, dict]:
    # Deduplicate tickets by content
    unique_tickets = {}
    for ticket in tickets:
        key = (
            ticket["substation"],
            ticket["mission"],
            tuple(text for _, text in ticket["entries"]),
        )
        unique_tickets.setdefault(key, ticket)
    tickets_unique = list(unique_tickets.values())

    # Deduplicate entries
    entry_to_stations = defaultdict(set)
    entry_to_tickets = defaultdict(set)
    entry_order = []
    for ticket in tickets_unique:
        tkey = f"{ticket['substation']}｜{ticket['mission']}"
        for _, text in ticket["entries"]:
            if not text:
                continue
            if text not in entry_to_stations:
                entry_order.append(text)
            entry_to_stations[text].add(ticket["substation"])
            entry_to_tickets[text].add(tkey)

    records = []
    for text in entry_order:
        rec = analyze_entry(text)
        rec["stations"] = "、".join(sorted(entry_to_stations[text]))
        rec["ticket_count"] = len(entry_to_tickets[text])
        records.append(rec)

    total_entries = len(records)
    normalized_unique = len({rec["norm"] for rec in records})
    stations = sorted({t["substation"] for t in tickets_unique if t["substation"]})
    missions = {(t["substation"], t["mission"]) for t in tickets_unique}
    entries_per_ticket = [len(t["entries"]) for t in tickets_unique]

    # Counters
    family_counter = Counter(rec["family"] for rec in records)
    first_action_counter = Counter(rec["first_action"] for rec in records if rec["first_action"])
    to_state_counter = Counter(t["to_state"] for rec in records for t in rec["transitions"])
    from_state_counter = Counter(
        t["from_raw"] for rec in records for t in rec["transitions"] if t["from_raw"]
    )
    suffix_counter = Counter(
        p["head"] for rec in records for p in rec["pairs"]
    )
    id_shape_counter = Counter(
        p["id_shape"] for rec in records for p in rec["pairs"]
    )
    voltage_counter = Counter(v for rec in records for v in rec["voltages"])
    glued_entries = [rec for rec in records if rec["glued"]]
    glued_counter = Counter(
        (item["token"], item["prefix"] + " + " + item["voltage"], item["ambiguous"])
        for rec in records
        for item in rec["glued"]
    )
    state_counter = Counter(w for rec in records for w in rec["states"])
    object_counter = Counter(w for rec in records for w in rec["objects"])
    place_counter = Counter(w for rec in records for w in rec["places"])
    scope_counter = Counter(w for rec in records for w in rec["scopes"])
    gap_type_counter = Counter(
        p["gap_type"] for rec in records for p in rec["pairs"]
    )
    named_device_counter = Counter(
        p["head"] for rec in records for p in rec["named_devices"]
    )
    named_object_counter = Counter(
        p["head"] for rec in records for p in rec["named_objects"]
    )

    # Coverage
    has_pair = sum(rec["has_pair"] for rec in records)
    has_named = sum(rec["has_named_device"] or rec["has_named_object"] for rec in records)
    has_target = sum(rec["has_target"] for rec in records)
    has_action = sum(rec["has_action"] for rec in records)
    kir_core = sum(rec["kir_core"] for rec in records)
    kir_core_place = sum(rec["kir_core_place"] for rec in records)
    kir_detail = sum(rec["kir_detail"] for rec in records)
    no_device = [rec for rec in records if not rec["has_pair"] and not rec["has_named_device"]]
    no_target = [rec for rec in records if rec["has_action"] and not rec["has_target"]]
    no_action = [rec for rec in records if not rec["has_action"]]
    multi_target = [rec for rec in records if rec["multi_target"]]
    letter_ids = [rec for rec in records if any(p["id_shape"] in {"alnum", "alnum_hyphen"} for p in rec["pairs"])]
    hash_ids = [rec for rec in records if any(p["id_shape"] == "hash" for p in rec["pairs"])]
    hyphen_ids = [rec for rec in records if any(p["id_shape"] in {"digit_hyphen", "alnum_hyphen"} for p in rec["pairs"])]
    roman_entries = [rec for rec in records if rec["has_roman"]]
    colon_entries = [rec for rec in records if rec["has_colon"]]
    range_entries = [rec for rec in records if rec["has_relation_range"]]
    panel_inside_entries = [rec for rec in records if rec["has_panel_inside"]]
    suspicious_entries = [rec for rec in records if rec["suspicious"]]
    zero_kv_entries = [rec for rec in records if rec["zero_kv"]]
    quote_entries = [rec for rec in records if rec["quote_imbalance"]]

    # Fragments: very short, or no action + no target + no state
    fragments = [
        rec for rec in records
        if rec["length"] <= 4
        or (not rec["has_action"] and not rec["has_target"] and not rec["states"] and not rec["places"])
    ]

    # Transition completeness
    transition_entries = [rec for rec in records if rec["transitions"]]
    by_without_transition = [
        rec for rec in records
        if "由" in rec["norm"] and not rec["transitions"]
    ]

    # Per-station coverage
    station_stats = {}
    for station in stations:
        station_recs = [
            rec for rec in records
            if station in entry_to_stations[rec["raw"]]
        ]
        station_stats[station] = {
            "entries": len(station_recs),
            "kir_core": sum(r["kir_core"] for r in station_recs),
            "pair": sum(r["has_pair"] for r in station_recs),
            "fragments": len([
                r for r in station_recs
                if r["length"] <= 4
                or (not r["has_action"] and not r["has_target"] and not r["states"] and not r["places"])
            ]),
        }

    # Per-family coverage
    family_stats = {}
    for family in ["状态转换", "检查确认", "执行操作", "其他"]:
        fam_recs = [r for r in records if r["family"] == family]
        family_stats[family] = {
            "entries": len(fam_recs),
            "kir_core": sum(r["kir_core"] for r in fam_recs),
            "pair": sum(r["has_pair"] for r in fam_recs),
            "no_target": sum(1 for r in fam_recs if not r["has_target"]),
        }

    # Duplicate entries inside a ticket
    duplicate_within_ticket = []
    for ticket in tickets_unique:
        seen = Counter(text for _, text in ticket["entries"] if text)
        for text, count in seen.items():
            if count > 1:
                duplicate_within_ticket.append((ticket, text, count))

    # Ticket-level issues
    ticket_issues = []
    for ticket in tickets_unique:
        m = ticket["mission"]
        reasons = []
        if re.search(r"(?<!\d)0kv", clean_text(m)):
            reasons.append("mission 0kv")
        if "确环" in m:
            reasons.append("mission 确环（疑为合环）")
        if "键顺控" in m:
            reasons.append("mission 缺“一”")
        if "合环号" in m:
            reasons.append("mission 合环号")
        if reasons:
            ticket_issues.append((ticket, reasons))

    # Uncovered examples
    uncovered = [
        rec for rec in records
        if not rec["has_target"] and rec["has_action"]
    ]

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------
    lines = []
    add = lines.append

    add("# 操作票语料分析报告（只读）")
    add("")
    add(f"语料目录：`{root}`")
    add("")
    add("> 本报告只做模式统计与覆盖率测量，不修改语料，也不修改项目代码。")
    add("")

    add("## 1. 语料概览")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---:|")
    add(f"| JSON 文件数 | {len(files)} |")
    add(f"| 可解析票数（按内容去重） | {len(tickets_unique)} |")
    add(f"| 票数（按站+任务去重） | {len(missions)} |")
    add(f"| 不重复条目（精确） | {total_entries} |")
    add(f"| 不重复条目（去空白/标点后） | {normalized_unique} |")
    add(f"| 站点数 | {len(stations)} |")
    add(f"| 条目/票 min/median/max | {min(entries_per_ticket)} / {median(entries_per_ticket)} / {max(entries_per_ticket)} |")
    add(f"| 解析失败文件 | {len(errors)} |")
    add("")
    add("### 站点分布")
    add("")
    add("| 站点 | 唯一条目 | KIR 核心覆盖 | ID+后缀 | 残片 |")
    add("|---|---:|---|---:|---:|")
    for station in stations:
        st = station_stats[station]
        add(f"| {station} | {st['entries']} | {st['kir_core']} ({pct(st['kir_core'], st['entries'])}) | {st['pair']} ({pct(st['pair'], st['entries'])}) | {st['fragments']} |")
    add("")

    if errors:
        add("### 解析失败文件")
        add("")
        for error in errors[:20]:
            add(f"- `{short(error, 100)}`")
        add("")

    add("## 2. 票面质量")
    add("")
    add("| 指标 | 条目数 | 占唯一条目 |")
    add("|---|---:|---:|")
    add(f"| 残片/不可判定 | {len(fragments)} | {pct(len(fragments), total_entries)} |")
    add(f"| 无动作 | {len(no_action)} | {pct(len(no_action), total_entries)} |")
    add(f"| 有动作但无目标 | {len(no_target)} | {pct(len(no_target), total_entries)} |")
    add(f"| 含疑似 OCR/录入噪声 | {len(suspicious_entries)} | {pct(len(suspicious_entries), total_entries)} |")
    add(f"| 引号不平衡 | {len(quote_entries)} | {pct(len(quote_entries), total_entries)} |")
    add(f"| 含 0kv | {len(zero_kv_entries)} | {pct(len(zero_kv_entries), total_entries)} |")
    add(f"| 含“由”但无状态转换 | {len(by_without_transition)} | {pct(len(by_without_transition), total_entries)} |")
    add(f"| 编号与电压粘连 | {len(glued_entries)} | {pct(len(glued_entries), total_entries)} |")
    add("")
    add("### 票/任务级问题")
    add("")
    if ticket_issues:
        for ticket, reasons in ticket_issues[:30]:
            add(f"- `{short(ticket['substation'] + '｜' + ticket['mission'], 100)}` — {'；'.join(reasons)}")
    else:
        add("（无）")
    add("")
    add("### 含“由”但未抽出状态转换")
    add("")
    add(examples(records, lambda r: r in by_without_transition, 15))
    add("")
    add("### 疑似 OCR/录入噪声")
    add("")
    token_counter = Counter(t for rec in records for t in rec["suspicious"])
    if token_counter:
        add("| 噪声词 | 建议正确形式 | 次数 |")
        add("|---|---|---:|")
        for token, count in token_counter.most_common():
            add(f"| `{token}` | {SUSPECT_TOKENS[token]} | {count} |")
        add("")
        add("示例：")
        add("")
        add(examples(records, lambda r: bool(r["suspicious"]), 15))
    else:
        add("（无）")
    add("")
    add("### 编号与电压粘连")
    add("")
    add("> 语料中存在 `1CLP235kV` 这种编号与电压无分隔的写法。按标准电压等级拆成 `1CLP2` + `35kV`；若同时存在多种拆法，标记为歧义。")
    add("")
    if glued_counter:
        add("| 粘连原文 | 建议拆分 | 歧义 | 次数 |")
        add("|---|---|---|---:|")
        for (token, split, ambiguous), count in glued_counter.most_common(25):
            add(f"| `{token}` | `{split}` | {'是' if ambiguous else '否'} | {count} |")
        add("")
        add("示例：")
        add("")
        add(examples(records, lambda r: bool(r["glued"]), 15))
    else:
        add("（无）")
    add("")
    add("### 残片/不可判定示例")
    add("")
    add(examples(records, lambda r: r in fragments, 20))
    add("")
    add("### 有动作但无目标示例")
    add("")
    add(examples(records, lambda r: r in no_target, 20))
    add("")
    add("### 无动作示例")
    add("")
    add(examples(records, lambda r: r in no_action, 20))
    add("")
    add("### 引号不平衡示例")
    add("")
    add(examples(records, lambda r: r in quote_entries, 12))
    add("")

    add("## 3. 设备名模式")
    add("")
    add("> 设备提及按“短语”切分：先在标点、`由/转/切至/至/在/及` 等关系词处切短语，再在短语内取**最右侧设备后缀**作为中心词，取中心词前**最近的编号**作为设备编号。这样 `#2主变110kV侧2-D10…把手` 不会被误配成 `#2+把手`。")
    add("")
    add("| 指标 | 条目数 | 占唯一条目 |")
    add("|---|---:|---:|")
    add(f"| 含 ID+设备后缀（强身份） | {has_pair} | {pct(has_pair, total_entries)} |")
    add(f"| 含命名设备/对象（无 ID） | {has_named} | {pct(has_named, total_entries)} |")
    add(f"| 含任一目标（ID 或命名） | {has_target} | {pct(has_target, total_entries)} |")
    add(f"| 无任何设备/对象/系统 | {len(no_device)} | {pct(len(no_device), total_entries)} |")
    add(f"| 多设备/多对象（≥2） | {len(multi_target)} | {pct(len(multi_target), total_entries)} |")
    add(f"| 含字母编号 | {len(letter_ids)} | {pct(len(letter_ids), total_entries)} |")
    add(f"| 含 `#` 编号 | {len(hash_ids)} | {pct(len(hash_ids), total_entries)} |")
    add(f"| 含连字符编号 | {len(hyphen_ids)} | {pct(len(hyphen_ids), total_entries)} |")
    add(f"| 含罗马数字间隔 | {len(roman_entries)} | {pct(len(roman_entries), total_entries)} |")
    add(f"| 含“至…之间”关系 | {len(range_entries)} | {pct(len(range_entries), total_entries)} |")
    add(f"| 含柜内/屏内/母排上 | {len(panel_inside_entries)} | {pct(len(panel_inside_entries), total_entries)} |")
    add(f"| 含冒号分隔（屏柜：目标） | {len(colon_entries)} | {pct(len(colon_entries), total_entries)} |")
    add("")
    add("### 设备中心词频次")
    add("")
    add(counter_table(suffix_counter, total_entries, 30))
    add("")
    add("### 编号形态")
    add("")
    add(counter_table(id_shape_counter, total_entries, 10))
    add("")
    add("### 电压等级")
    add("")
    add("> 同一 entry 可能含多个电压等级，因此各电压的“占唯一条目”之和可超过 100%。")
    add("")
    add(counter_table(voltage_counter, total_entries, 10))
    add("")
    add("### ID 与后缀之间的间隔类型")
    add("")
    add(counter_table(gap_type_counter, total_entries, 10))
    add("")
    add("### 命名设备（无 ID）")
    add("")
    add(counter_table(named_device_counter, total_entries, 20))
    add("")
    add("### 命名对象（无 ID）")
    add("")
    add(counter_table(named_object_counter, total_entries, 20))
    add("")
    add("### 字母/连字符/井号编号示例")
    add("")
    add("**字母编号：**")
    add("")
    add(examples(records, lambda r: r in letter_ids, 15))
    add("")
    add("**连字符编号：**")
    add("")
    add(examples(records, lambda r: r in hyphen_ids, 15))
    add("")
    add("**井号编号：**")
    add("")
    add(examples(records, lambda r: r in hash_ids, 12))
    add("")
    add("**多设备/复合短语：**")
    add("")
    add(examples(records, lambda r: r in multi_target, 15))
    add("")
    add("**无目标示例：**")
    add("")
    add(examples(records, lambda r: r in no_device, 20))
    add("")

    add("## 4. 关键信息（KIR）模式")
    add("")
    add("> KIR 核心 = 有动作（执行/验证/状态转换）+ 有目标（ID 设备、命名设备、命名对象或系统）。KIR 核心（含场所）= 目标或场所（检查现场/后台机/顺控主机等条件类条目）。KIR 明细 = 核心 + 明确状态/位置/场所。")
    add("")
    add("| 指标 | 条目数 | 占唯一条目 |")
    add("|---|---:|---:|")
    add(f"| 有动作 | {has_action} | {pct(has_action, total_entries)} |")
    add(f"| 有目标 | {has_target} | {pct(has_target, total_entries)} |")
    add(f"| **KIR 核心可抽取** | **{kir_core}** | **{pct(kir_core, total_entries)}** |")
    add(f"| KIR 核心（含场所）可抽取 | {kir_core_place} | {pct(kir_core_place, total_entries)} |")
    add(f"| KIR 明细可抽取 | {kir_detail} | {pct(kir_detail, total_entries)} |")
    add("")
    add("### 操作族分布")
    add("")
    add("| 操作族 | 条目数 | KIR 核心 | ID+后缀 | 无目标 |")
    add("|---|---:|---|---:|---:|")
    for family, st in family_stats.items():
        add(f"| {family} | {st['entries']} | {st['kir_core']} ({pct(st['kir_core'], st['entries'])}) | {st['pair']} ({pct(st['pair'], st['entries'])}) | {st['no_target']} |")
    add("")
    add("### 动作动词频次")
    add("")
    add(counter_table(first_action_counter, total_entries, 25))
    add("")
    add("### 状态转换：目标状态")
    add("")
    add(counter_table(to_state_counter, total_entries, 15))
    add("")
    add("### 状态转换：起始侧原文")
    add("")
    add(counter_table(from_state_counter, total_entries, 20))
    add("")
    add("### 状态词频次")
    add("")
    add(counter_table(state_counter, total_entries, 25))
    add("")
    add("### 对象/功能频次")
    add("")
    add(counter_table(object_counter, total_entries, 25))
    add("")
    add("### 场所频次")
    add("")
    add(counter_table(place_counter, total_entries, 20))
    add("")
    add("### 系统/范围词频次")
    add("")
    add(counter_table(scope_counter, total_entries, 10))
    add("")
    add("### KIR 核心缺失示例（有动作无目标）")
    add("")
    add(examples(records, lambda r: r in uncovered, 25))
    add("")

    add("## 5. 结论与建议")
    add("")
    pair_pct = pct(has_pair, total_entries)
    target_pct = pct(has_target, total_entries)
    kir_pct = pct(kir_core, total_entries)
    add(f"1. **设备名语法覆盖率**：{has_pair}/{total_entries}（{pair_pct}）的唯一条目能抽出 ID+设备后缀；{has_target}/{total_entries}（{target_pct}）至少能抽出一个目标（含命名设备/对象/系统）。")
    add(f"2. **KIR 核心覆盖率**：{kir_core}/{total_entries}（{kir_pct}）能抽出“动作+目标”；把场所（现场/后台机/顺控主机等条件类检查）也算作目标时为 {kir_core_place}/{total_entries}（{pct(kir_core_place, total_entries)}）。其余主要是残片、纯状态描述、纯目标描述或票面噪声。")
    add(f"3. **票面噪声**：{len(suspicious_entries)} 条含疑似 OCR/录入噪声，{len(quote_entries)} 条引号不平衡，{len(zero_kv_entries)} 条含 0kv。票面即真值的前提是票面文本必须是官方原文；若语料来自 OCR，需先做票面勘误或把噪声条目标记为不可判定。")
    add(f"4. **复杂结构**：{len(multi_target)} 条含多设备/多对象，{len(range_entries)} 条含“至…之间”，{len(colon_entries)} 条含冒号分隔的屏柜/目标，{len(letter_ids)} 条含字母编号。这些必须用短语切分 + 最近编号关联，不能只靠一条全局正则。")
    add(f"5. **状态转换**：{len(transition_entries)} 条能抽出状态转换，目标状态分布见上表。状态转换条目的 KIR 核心覆盖率为 {pct(sum(r['kir_core'] for r in transition_entries), len(transition_entries)) if transition_entries else '0.0%'}。")
    add("")
    add("**建议**：")
    add("")
    add("- 以“短语切分 + 中心词 + 最近编号”为设备语法主干，再补电压/间隔/修饰字段；不要退回单条整句正则。")
    add("- 对无 ID 的命名设备/对象（母线保护、软压板、接地线、系统）单独建类，不强行要求编号。")
    add("- 严格档只对 KIR 核心/明细做精确比较；非关键信息只用于对齐，不参与判定。")
    add("- 票面噪声必须单列：加载时对“残片/无动作无目标/引号不平衡/0kv/疑似噪声”告警，必要时标记为不可判定，而不是静默判失败。")
    add("- 下一步可用同一脚本把每条票的 KIR 草稿导出，人工抽查 50~100 条，确定“检查/验明”“由…转…”等族的必填字段。")
    add("")

    report = "\n".join(lines)

    stats = {
        "corpus_dir": str(root),
        "files": len(files),
        "errors": errors,
        "tickets_unique": len(tickets_unique),
        "missions": len(missions),
        "stations": stations,
        "entries_unique": total_entries,
        "entries_normalized_unique": normalized_unique,
        "entries_per_ticket": {
            "min": min(entries_per_ticket),
            "median": median(entries_per_ticket),
            "max": max(entries_per_ticket),
        },
        "coverage": {
            "has_pair": has_pair,
            "has_named": has_named,
            "has_target": has_target,
            "kir_core": kir_core,
            "kir_core_place": kir_core_place,
            "kir_detail": kir_detail,
            "has_action": has_action,
        },
        "family": family_stats,
        "station": station_stats,
        "counters": {
            "suffix": dict(suffix_counter),
            "id_shape": dict(id_shape_counter),
            "voltage": dict(voltage_counter),
            "gap_type": dict(gap_type_counter),
            "named_device": dict(named_device_counter),
            "named_object": dict(named_object_counter),
            "first_action": dict(first_action_counter),
            "to_state": dict(to_state_counter),
            "from_state": dict(from_state_counter),
            "state": dict(state_counter),
            "object": dict(object_counter),
            "place": dict(place_counter),
            "scope": dict(scope_counter),
            "suspicious": dict(token_counter),
        },
        "glued_voltage": [
            {"token": token, "split": split, "ambiguous": ambiguous, "count": count}
            for (token, split, ambiguous), count in glued_counter.most_common(50)
        ],
        "anomalies": {
            "fragments": len(fragments),
            "no_action": len(no_action),
            "no_target": len(no_target),
            "suspicious_entries": len(suspicious_entries),
            "quote_imbalance": len(quote_entries),
            "zero_kv": len(zero_kv_entries),
            "by_without_transition": len(by_without_transition),
            "multi_target": len(multi_target),
            "letter_ids": len(letter_ids),
            "hash_ids": len(hash_ids),
            "hyphen_ids": len(hyphen_ids),
            "roman_entries": len(roman_entries),
            "range_entries": len(range_entries),
            "colon_entries": len(colon_entries),
            "panel_inside_entries": len(panel_inside_entries),
            "duplicate_within_ticket": len(duplicate_within_ticket),
            "ticket_issues": len(ticket_issues),
            "glued_entries": len(glued_entries),
            "glued_ambiguous_entries": sum(1 for r in records if r["glued_ambiguous"]),
        },
        "ticket_issues": [
            {
                "substation": ticket["substation"],
                "mission": ticket["mission"],
                "reasons": reasons,
            }
            for ticket, reasons in ticket_issues
        ],
        "samples": {
            "uncovered": [r["raw"] for r in uncovered[:50]],
            "fragments": [r["raw"] for r in fragments[:50]],
            "suspicious": [r["raw"] for r in suspicious_entries[:50]],
            "letter_ids": [r["raw"] for r in letter_ids[:50]],
            "multi_target": [r["raw"] for r in multi_target[:50]],
            "glued": [r["raw"] for r in glued_entries[:50]],
        },
    }
    return report, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus_dir", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("."))
    args = parser.parse_args()

    root = args.corpus_dir
    if not root.is_dir():
        raise SystemExit(f"corpus dir not found: {root}")

    files, tickets, errors = load_corpus(root)
    report, stats = build_report(root, files, tickets, errors)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.md"
    stats_path = out_dir / "stats.json"
    report_path.write_text(report, encoding="utf-8")
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"files={len(files)} tickets={stats['tickets_unique']} entries={stats['entries_unique']}")
    print(f"pair={stats['coverage']['has_pair']} target={stats['coverage']['has_target']} kir_core={stats['coverage']['kir_core']}")
    print(f"report={report_path}")
    print(f"stats={stats_path}")


if __name__ == "__main__":
    main()
