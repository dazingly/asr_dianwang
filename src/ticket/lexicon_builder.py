# -*- coding: utf-8 -*-
"""从真实操作票构建站点结构化词典和设备台账。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT

_ACTIONS = (
    "检查", "核对", "确认", "验明", "合上", "拉开", "投入", "停用",
    "切至", "送电", "停电", "复归",
)
_STATES = (
    "冷备用", "热备用", "运行", "检修", "分闸", "合闸", "远方", "就地",
    "并列", "解列", "投入", "停用", "合好", "拉开", "正确", "无异常信号",
    "无电压", "具备操作条件",
)
_PANEL_TERMS = (
    "桥保护测控", "111保护测控", "110kV备投屏", "110kV电压并列屏",
    "汇控柜", "测控屏", "保护屏", "主控屏",
)
_HANDLE_TERMS = (
    "操作方式把手", "电压并列切换把手", "远方就地把手", "切换把手", "把手",
)
_ASSET_END_MARKERS = (
    "电气位置指示", "机械位置指示", "电压指示", "确在", "确已",
    "无异常信号", "线路侧三相", "由“", '由"', "由远方", "由就地",
    "由解列", "由并列",
)
_ASSET_SUFFIXES = (
    "接地刀闸", "隔离开关", "空气开关", "开关", "刀闸", "保护压板",
    "投入压板", "出口压板", "压板", "把手", "保护装置", "母线",
)
_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"\d+-\d+[A-Za-z]+\d+|"
    r"\d+[A-Za-z]+\d+|"
    r"\d+-[A-Za-z]?\d+|"
    r"#\d+PT|"
    r"P\d+"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PLAIN_DEVICE_CODE_RE = re.compile(r"(?<!\d)(\d{3})(?=(?:开关|刀闸|间隔))")
# 现场命名规律是“电压等级 + 线路名 + 编号”，例如 110kV丹东线111、35kV黄堽线312，
# 这里只按规律匹配，不绑定任何具体站名。
_BAY_PATTERNS = (
    # 母线：#编号母线，可带电压等级前缀
    re.compile(r"(?:\d+(?:\.\d+)?\s*[kK][vV])?#\d+母线(?:PT)?", re.IGNORECASE),
    # 间隔：线路名 + 编号 + 间隔，或 #编号PT 间隔
    re.compile(r"(?:[一-鿿]{1,4}线(?:路)?\d*|#\d+PT)间隔", re.IGNORECASE),
    # 线路名。左侧会粘连动词和屏柜名（“检查黄堽线312开关”），
    # 由 _clean_bay_term 剥离；接地线、短路线这类非线路名由 _is_bay_term 剔除。
    re.compile(r"(?:\d+(?:\.\d+)?\s*[kK][vV])?[一-鿿]{1,4}线(?:路)?(?:\d{3})?"),
)

# 线路名左侧经常直接粘连的动词、屏柜名
_BAY_LEFT_WORDS = (
    "检查", "核对", "确认", "验明", "合上", "拉开", "投入", "停用", "将",
    "汇控柜", "测控屏", "保护屏", "主控屏", "开关柜",
)
# 以及单字的连接词、方位词，和“控柜/控屏”这类被贪婪匹配切掉一半的屏柜名
_BAY_LEFT_CHARS = "柜屏箱盘控内在和与及由至对从的"

# 剥离左侧粘连后仍不是线路名的“线”字尾巴
_BAY_STOP_TAILS = (
    "接地线", "短路线", "进线", "出线", "二次线", "控制线",
    "电缆线", "地线", "引线", "导线", "联络线", "线路侧",
)
# 线路名主体不允许以这些设备词开头
_BAY_STOP_HEADS = ("刀闸", "开关", "接地", "短路", "电流", "电压", "压板", "把手")


def build_station_lexicon(ticket_dir: str | Path) -> dict:
    """读取目录内全部票据，返回可追溯、稳定排序的站点词典。"""
    root = Path(ticket_dir)
    paths = sorted(root.glob("*.json"), key=_ticket_sort_key)
    if not paths:
        raise ValueError(f"没有找到操作票: {root}")

    terms: dict[str, dict[str, dict]] = defaultdict(dict)
    ticket_count = 0
    entry_count = 0

    def add(category: str, term: str, source: str) -> None:
        value = _clean_term(term)
        if not value:
            return
        record = terms[category].setdefault(
            value, {"term": value, "count": 0, "sources": set()}
        )
        record["count"] += 1
        record["sources"].add(source)

    for path in paths:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        entries = data.get("entries")
        if not isinstance(entries, dict):
            raise ValueError(f"{path} 缺少 entries 对象")

        ticket_count += 1
        ticket_name = path.stem
        substation = str(data.get("substation", ""))
        mission = str(data.get("mission", ""))
        add("substations", substation, ticket_name)
        add("missions", mission, ticket_name)

        for seq, text in sorted(entries.items(), key=lambda item: int(item[0])):
            entry = str(text)
            source = f"{ticket_name}#{seq}"
            entry_count += 1
            _extract_entry_terms(entry, source, add)
            for voltage in re.findall(r"\d+(?:\.\d+)?\s*[kK][vV]", entry):
                add("voltage_levels", _canonical_voltage(voltage), source)

    categories = {}
    for category in (
        "substations", "missions", "voltage_levels", "lines_and_bays",
        "devices", "device_codes", "protection_plates", "panels", "handles",
        "actions", "positions_and_states",
    ):
        records = terms.get(category, {})
        categories[category] = [
            {
                "term": record["term"],
                "count": record["count"],
                "sources": sorted(record["sources"], key=_source_sort_key),
            }
            for record in sorted(
                records.values(), key=lambda item: (-len(item["term"]), item["term"])
            )
        ]

    stations = categories["substations"]
    station = stations[0]["term"] if len(stations) == 1 else ""
    return {
        "schema_version": 1,
        "station": station,
        "source": f"{_display_path(root)}/*.json",
        "ticket_count": ticket_count,
        "entry_count": entry_count,
        "categories": categories,
    }


def asset_terms(lexicon: dict) -> list[str]:
    """提取供 SlotExtractor 读取的一行一词设备台账。"""
    categories = lexicon["categories"]
    values = {
        item["term"]
        for category in (
            "lines_and_bays", "devices", "device_codes",
            "protection_plates", "handles",
        )
        for item in categories.get(category, [])
        if (
            _is_device_code(item["term"])
            if category == "device_codes"
            else _is_specific_asset(item["term"])
        )
    }
    return sorted(values, key=lambda term: (-len(term), term))


def write_station_lexicon(
    lexicon: dict,
    yaml_path: str | Path,
    asset_path: str | Path,
) -> None:
    """写出结构化 YAML 和匹配器兼容的纯文本台账。"""
    yaml_target = Path(yaml_path)
    asset_target = Path(asset_path)
    yaml_target.parent.mkdir(parents=True, exist_ok=True)
    asset_target.parent.mkdir(parents=True, exist_ok=True)

    with open(yaml_target, "w", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(
            lexicon, stream, allow_unicode=True, sort_keys=False, width=120
        )

    station = lexicon.get("station") or "本站"
    header = (
        f"# 由 scripts/build_station_lexicon.py 从{station}操作票自动生成。\n"
        "# 一行一个设备、间隔、压板或把手全称；请勿手工维护。\n"
    )
    with open(asset_target, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(header)
        stream.write("\n".join(asset_terms(lexicon)))
        stream.write("\n")


def _extract_entry_terms(entry: str, source: str, add) -> None:
    for action in _ACTIONS:
        if action in entry:
            add("actions", action, source)
    for state in _STATES:
        if state in entry:
            add("positions_and_states", state, source)
    for panel in _PANEL_TERMS:
        if panel in entry:
            add("panels", panel, source)
    for handle in _HANDLE_TERMS:
        if handle in entry:
            add("handles", handle, source)
    for pattern in _BAY_PATTERNS:
        for match in pattern.finditer(entry):
            term = _clean_bay_term(match.group(0))
            if _is_bay_term(term):
                add("lines_and_bays", term, source)
    for match in _CODE_RE.finditer(entry):
        add("device_codes", match.group(1).upper(), source)
    for match in _PLAIN_DEVICE_CODE_RE.finditer(entry):
        add("device_codes", match.group(1), source)

    for target in _extract_asset_targets(entry):
        add("devices", target, source)
        if "压板" in target:
            add("protection_plates", target, source)
        core = re.sub(r"^(?:汇控柜|测控屏)", "", target)
        if core != target:
            add("devices", core, source)


def _extract_asset_targets(entry: str) -> list[str]:
    text = entry.strip("。；; ")
    text = re.sub(r"^(?:检查|核对|确认|验明|合上|拉开|投入|停用|将)", "", text)
    panel = ""
    for candidate in ("测控屏", "汇控柜"):
        if text.startswith(candidate):
            panel = candidate
            text = text[len(candidate):]
            break
    text = re.sub(r"^(?:检查|核对|确认|验明|合上|拉开|投入|停用|将)", "", text)
    text = panel + text

    end = len(text)
    for marker in _ASSET_END_MARKERS:
        index = text.find(marker)
        if index >= 0:
            end = min(end, index)
    target = text[:end].strip(" ，,、")
    target = re.sub(r"(?:两只|一只|三只)$", "", target)

    if not target or target in {"现场", "送电区域内", "系统"}:
        return []
    if not (
        any(suffix in target for suffix in _ASSET_SUFFIXES)
        or _CODE_RE.search(target)
        or _PLAIN_DEVICE_CODE_RE.search(target)
    ):
        return []
    return [target]


def _clean_bay_term(term: str) -> str:
    """剥掉线路/间隔名左侧粘连的动词、屏柜名和连接词。"""
    changed = True
    while changed and term:
        changed = False
        for word in _BAY_LEFT_WORDS:
            if term.startswith(word) and len(term) > len(word):
                term = term[len(word):]
                changed = True
                break
        if not changed and len(term) > 1 and term[0] in _BAY_LEFT_CHARS:
            term = term[1:]
            changed = True
    return term


def _is_bay_term(term: str) -> bool:
    """剔除“接地线”“短路线”这类不是线路名的“线”字词。"""
    if len(term) < 3:
        return False
    if term.startswith("#"):
        return True
    if any(term.startswith(head) for head in _BAY_STOP_HEADS):
        return False
    core = re.sub(r"^\d+(?:\.\d+)?\s*[kK][vV]", "", term, flags=re.IGNORECASE)
    # 线路名后面常跟三位间隔编号（黄堽线312），判断主体时要先去掉
    core = re.sub(r"\d{3}[A-Za-z]?\d*$", "", core)
    if any(core.endswith(tail) for tail in _BAY_STOP_TAILS):
        return False
    return core.endswith(("线", "线路", "间隔", "母线", "PT"))


def _is_specific_asset(term: str) -> bool:
    generic = {
        "开关", "刀闸", "压板", "把手", "保护装置", "母线",
        "汇控柜", "测控屏",
    }
    return term not in generic and len(term) >= 4 and not term.startswith("#")


def _is_device_code(term: str) -> bool:
    return len(term) >= 3 and not term.startswith("#") and not term.isdigit()


def _clean_term(term: str) -> str:
    return re.sub(r"\s+", " ", term).strip(" ，,。；;、“”\"'")


def _display_path(path: Path) -> str:
    """能相对项目根就相对，否则退回绝对路径，用于写出来源说明。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _canonical_voltage(value: str) -> str:
    return re.sub(r"\s*[kK][vV]$", "kV", value.strip())


def _ticket_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.stem)
    return (int(match.group(1)) if match else 10**9, path.name)


def _source_sort_key(source: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"ticket(\d+)(?:#(\d+))?", source)
    if not match:
        return (10**9, 10**9, source)
    return (int(match.group(1)), int(match.group(2) or 0), source)
