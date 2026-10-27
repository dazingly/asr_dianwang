# -*- coding: utf-8 -*-
"""KIR 抽取的回归用例。

这些用例直接对应 docs/design/kir_schema_and_extraction.md 的典型解析，
以及语料抽查中暴露过的几类错误：
  - 场所/系统被冒号或“及”拆断
  - 线路名吞掉压板编号、电压粘连
  - 刀闸开关侧、合好、确在屏内
  - 名称里的“送电/分列”被误当动作/状态
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.ticket.kir import Family, KirExtractor, TargetKind

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "kir.yaml"


@pytest.fixture(scope="module")
def extractor() -> KirExtractor:
    return KirExtractor(str(CONFIG))


def _targets(kir, kind: TargetKind | None = None, suffix: str | None = None):
    out = kir.targets
    if kind is not None:
        out = [t for t in out if t.kind is kind]
    if suffix is not None:
        out = [t for t in out if t.suffix == suffix]
    return out


def _state_values(kir) -> list[str]:
    return [s.value for s in kir.states]


def _flag_codes(kir) -> list[str]:
    return [f.code for f in kir.quality.flags]


def test_execute_switch(extractor: KirExtractor) -> None:
    kir = extractor.extract("拉开测控屏35kV母线分段300开关", source="ticket")
    assert kir.family is Family.EXECUTE
    assert [a.value for a in kir.actions] == ["拉开"]
    target = kir.targets[0]
    assert target.kind is TargetKind.DEVICE
    assert target.suffix == "开关"
    assert target.place == "测控屏"
    assert target.voltage == "35kv"
    assert target.interval == "母线分段"
    assert target.id is not None
    assert target.id.raw == "300"
    assert target.id.core == "300"
    assert kir.residual == ""


def test_verify_indication(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查测控屏35kV母线分段300开关电气位置指示确已拉开", source="ticket"
    )
    assert kir.family is Family.VERIFY
    assert [a.value for a in kir.actions] == ["检查"]
    assert [(s.value, s.confirmation) for s in kir.states] == [("拉开", "确已")]
    assert len(_targets(kir, TargetKind.DEVICE, "开关")) == 1
    indication = _targets(kir, TargetKind.OBJECT, "电气位置指示")
    assert len(indication) == 1
    assert indication[0].place == "测控屏"
    assert indication[0].id is not None and indication[0].id.core == "300"
    assert kir.residual == ""


def test_design_grounding_example(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查黄堽线312-1刀闸开关侧确已装设#4接地线一组", source="ticket"
    )
    knife = _targets(kir, TargetKind.DEVICE, "刀闸")[0]
    assert knife.interval == "黄堽线"
    assert knife.id is not None
    assert knife.id.core == "3121"
    assert knife.id.segments == ("312", "1")
    assert "开关侧" in knife.qualifiers

    ground = _targets(kir, TargetKind.DEVICE, "接地线")[0]
    assert ground.id is not None and ground.id.core == "4"

    assert [(s.value, s.confirmation) for s in kir.states] == [("装设", "确已")]
    assert [(q.number, q.unit) for q in kir.quantities] == [(1, "组")]
    assert kir.residual == ""


def test_handle_transition(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "将测控屏110kV桥100开关操作方式把手由“远方”切至“就地”位置",
        source="ticket",
    )
    assert kir.family is Family.TRANSITION
    assert kir.transition is not None
    assert (kir.transition.src, kir.transition.dst) == ("远方", "就地")
    handle = _targets(kir, TargetKind.DEVICE, "把手")[0]
    assert handle.place == "测控屏"
    assert handle.voltage == "110kv"
    assert handle.interval == "桥"
    assert handle.id is not None and handle.id.raw == "100"
    assert "开关操作方式" in handle.qualifiers
    assert kir.residual == ""


def test_multi_target_transition(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "将#2主变110kV侧102开关、35kV侧302开关、10kV侧002开关"
        "由冷备用转热备用",
        source="ticket",
    )
    assert kir.family is Family.TRANSITION
    switches = _targets(kir, TargetKind.DEVICE, "开关")
    assert [t.id.raw for t in switches if t.id] == ["102", "302", "002"]
    assert [t.voltage for t in switches] == ["110kv", "35kv", "10kv"]
    assert switches[0].interval == "#2主变"
    assert kir.transition is not None
    assert (kir.transition.src, kir.transition.dst) == ("冷备用", "热备用")
    assert kir.residual == ""


def test_ocr_scope_and_object_are_split(extractor: KirExtractor) -> None:
    kir = extractor.extract("检查35kV系统符合确环条件", source="ticket")
    assert kir.family is Family.VERIFY
    assert "OCR_SUSPECT" in _flag_codes(kir)
    scope = _targets(kir, TargetKind.SCOPE)[0]
    assert scope.surface == "35kv系统"
    assert scope.scope == "35kv系统"
    assert scope.voltage == "35kv"
    obj = _targets(kir, TargetKind.OBJECT)[0]
    assert obj.surface == "条件"
    assert "符合" in _state_values(kir)
    assert "合环" in _state_values(kir)


def test_ocr_only_on_ticket_side(extractor: KirExtractor) -> None:
    ticket = extractor.extract("检查35kV系统符合确环条件", source="ticket")
    asr = extractor.extract("检查35kV系统符合确环条件", source="asr")
    assert "OCR_SUSPECT" in _flag_codes(ticket)
    assert "OCR_SUSPECT" not in _flag_codes(asr)


def test_ocr_plate_name(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "投入后台机35kV母线保护分留母联分刘软压板", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "软压板")[0]
    assert target.place == "后台机"
    assert target.voltage == "35kv"
    assert target.name == "母线保护分裂母联分裂"
    assert _flag_codes(kir).count("OCR_SUSPECT") == 2
    assert kir.residual == ""


def test_object_place_and_scope_split(extractor: KirExtractor) -> None:
    kir = extractor.extract("检查顺控主机无异常信号", source="ticket")
    assert [t.kind for t in kir.targets] == [TargetKind.PLACE, TargetKind.OBJECT]
    assert [t.surface for t in kir.targets] == ["顺控主机", "信号"]
    assert "无异常" in _state_values(kir)

    kir = extractor.extract("检查现场具备操作条件", source="ticket")
    assert [t.kind for t in kir.targets] == [TargetKind.PLACE, TargetKind.OBJECT]
    assert [t.surface for t in kir.targets] == ["现场", "条件"]
    assert "具备" in _state_values(kir)
    assert "操作" in kir.targets[1].qualifiers


def test_scope_power_supply_area(extractor: KirExtractor) -> None:
    kir = extractor.extract("检查送电区域内确无接地短路线", source="ticket")
    assert [a.value for a in kir.actions] == ["检查"]
    scope = _targets(kir, TargetKind.SCOPE)[0]
    assert scope.surface == "送电区域"
    assert scope.scope == "送电区域"
    assert "无接地短路线" in _state_values(kir)
    assert kir.residual == ""


def test_pending_place_before_colon(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "停用#2主变保护测控屏：2-2CLP6中后备保护跳300开关压板",
        source="ticket",
    )
    assert kir.family is Family.EXECUTE
    assert len(kir.targets) == 1
    target = kir.targets[0]
    assert target.suffix == "压板"
    assert target.place == "#2主变保护测控屏"
    assert target.id is not None and target.id.raw == "2-2clp6"
    assert kir.residual == ""


def test_pending_place_with_and(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "将公用测控及电压并列屏：电压并列把手由“分列”切至“并列”位置",
        source="ticket",
    )
    target = _targets(kir, TargetKind.DEVICE, "把手")[0]
    assert target.place == "公用测控及电压并列屏"
    assert kir.transition is not None
    assert (kir.transition.src, kir.transition.dst) == ("分列", "并列")
    assert kir.residual == ""


def test_line_name_does_not_eat_plate_id(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查2-RLP2111仿河线差动保护投入压板确在投入位置", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "压板")[0]
    assert target.interval == "仿河线"
    assert target.id is not None and target.id.raw == "2-rlp2"
    assert "111" in target.qualifiers
    assert [(s.value, s.confirmation) for s in kir.states] == [("投入位置", "确在")]
    assert kir.residual == ""


def test_glued_voltage(extractor: KirExtractor) -> None:
    kir = extractor.extract("1CLP210KV备投合000开关压板", source="ticket")
    target = _targets(kir, TargetKind.DEVICE, "压板")[0]
    assert target.voltage == "10kv"
    assert target.id is not None and target.id.raw == "1clp2"
    assert "备投合000开关" in target.qualifiers


def test_segment_interval(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "10kV分段1010开关柜1CLP1010开关跳闸出口压板", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "跳闸出口压板")[0]
    assert target.voltage == "10kv"
    assert target.interval == "分段1010"
    assert target.place == "开关柜"
    assert target.id is not None and target.id.raw == "1clp10"
    assert "10开关" in target.qualifiers


def test_confirmed_place_is_a_state(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查35kV母线保护相关保护压板确在35kV母线保护屏内", source="ticket"
    )
    assert any(
        s.value == "在" and s.confirmation == "确在" for s in kir.states
    )
    assert "INCOMPLETE_VERIFY" not in _flag_codes(kir)


def test_confirmed_transition_is_not_exec_action(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查#2主变10kV侧002开关手车确已推至试验位置", source="ticket"
    )
    assert [a.value for a in kir.actions] == ["检查"]
    assert "推至" in _state_values(kir)
    assert "试验位置" in _state_values(kir)


def test_position_word_inside_plate_name(extractor: KirExtractor) -> None:
    kir = extractor.extract("投入1KLP9母联310分列压板", source="ticket")
    assert "分列" not in _state_values(kir)
    target = _targets(kir, TargetKind.DEVICE, "压板")[0]
    assert target.id is not None and target.id.raw == "1klp9"
    assert "310分列" in target.qualifiers


def test_pt_is_not_a_separate_target(extractor: KirExtractor) -> None:
    kir = extractor.extract("拉开110kV#2PTPL12刀闸", source="ticket")
    assert len(kir.targets) == 1
    target = kir.targets[0]
    assert target.suffix == "刀闸"
    assert target.id is not None and target.id.raw == "#2ptpl12"


def test_fragment_quality(extractor: KirExtractor) -> None:
    kir = extractor.extract("内", source="ticket")
    assert kir.family is Family.OTHER
    assert "FRAGMENT" in _flag_codes(kir)


def test_phase_qualifier_after_device(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查#2主变35kV侧302-1刀闸三相确在分闸位置", source="ticket"
    )
    knife = _targets(kir, TargetKind.DEVICE, "刀闸")[0]
    assert "3相" in knife.qualifiers
    assert kir.residual == ""


def test_indirect_voltage_check_is_action(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查#2主变35kV侧302-1刀闸机械位置指示确在分闸位置（间接验电）",
        source="ticket",
    )
    assert "验电" in [a.value for a in kir.actions]
    assert kir.residual == ""


def test_place_before_action(extractor: KirExtractor) -> None:
    kir = extractor.extract("测控屏合上#2主变10kV侧002开关", source="ticket")
    target = _targets(kir, TargetKind.DEVICE, "开关")[0]
    assert target.place == "测控屏"
    assert kir.residual == ""


def test_confirmation_before_quoted_state(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查测控屏#2主变10kV侧002开关操作方式把手确在“就地”位置",
        source="ticket",
    )
    assert [(s.value, s.confirmation) for s in kir.states] == [("就地", "确在")]
    assert kir.residual == ""


def test_id_list_is_kept_as_interval(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查#1、#2主变10kV侧并列后负荷分配指示正确", source="ticket"
    )
    assert kir.targets[0].interval == "#1、#2主变"
    assert kir.residual == ""


def test_fentu_is_location(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "投入后台机35kV母线保护分图内软压板", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "软压板")[0]
    assert target.place == "后台机"
    assert target.location == "分图内"
    assert kir.residual == ""


def test_place_list_before_screen(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查110kV菏铁Ⅰ线线路及119开关相关保护压板确在"
        "110kV菏铁Ⅰ线、仿朱线保护屏内",
        source="ticket",
    )
    place = _targets(kir, TargetKind.PLACE)[0]
    assert "110kv菏铁 1 线" in place.place
    assert "仿朱线" in place.place
    assert kir.residual == ""


def test_line_before_action_is_carried(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查2-RLP5111仿河线停用重合闸压板确在退出位置", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "压板")[0]
    assert target.interval == "仿河线"
    assert target.id is not None and target.id.raw == "2-rlp5"
    assert "111" in target.qualifiers
    assert kir.residual == ""


def test_line_interval_does_not_eat_qualifier(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "停用1-31CLP8备投合112仿香线开关压板", source="ticket"
    )
    target = _targets(kir, TargetKind.DEVICE, "压板")[0]
    assert target.interval == "仿香线"
    assert "备投合112" in target.qualifiers
    assert target.id is not None and target.id.raw == "1-31clp8"


def test_real_device_before_indicator(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查汇控柜#2主变110kV侧102-D1接地刀闸电气位置指示确已拉开",
        source="ticket",
    )
    device = _targets(kir, TargetKind.DEVICE, "接地刀闸")[0]
    assert device.id is not None and device.id.raw == "102-d1"
    assert device.place == "汇控柜"
    assert kir.residual == ""


def test_embedded_target_does_not_take_lead_voltage(
    extractor: KirExtractor,
) -> None:
    kir = extractor.extract(
        "在#2主变10kV侧002开关柜电缆室母线排装设#（1）接地线一组",
        source="ticket",
    )
    wire = _targets(kir, TargetKind.DEVICE, "接地线")[0]
    bar = _targets(kir, TargetKind.DEVICE, "母线排")[0]
    assert bar.voltage == "10kv"
    assert wire.voltage is None
    assert wire.interval is None


def test_ticket_leading_number_is_noise(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "13检查110kV系统合环后负荷分配指示正确", source="ticket"
    )
    assert kir.parse_text.startswith("检查")
    assert kir.residual == ""
    assert kir.family is Family.VERIFY


@pytest.mark.parametrize(("text", "plate", "tail"), [
    ("停用1C12LP13110开关跳闸出口压板", "1c12lp13", "110"),
    ("停用1C1LP1310开关跳闸出口压板", "1c1lp13", "10"),
    ("停用52LP273112开关跳闸出口压板", "52lp273", "112"),
    ("检查6YLP1102-1刀闸遥控压板确在投入位置", "6ylp1", "102-1"),
])
def test_plate_id_split_keeps_switch_number(
    extractor: KirExtractor, text: str, plate: str, tail: str,
) -> None:
    kir = extractor.extract(text, source="ticket")
    target = _targets(kir, TargetKind.DEVICE)[0]
    assert target.id is not None and target.id.raw == plate
    assert any(tail in q for q in target.qualifiers)
    assert kir.residual == ""


def test_standby_power_is_not_state(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查35kV备投电源自投装置确在投入位置", source="ticket"
    )
    assert _state_values(kir) == ["投入位置"]


def test_standby_word_in_device_name_is_not_state(
    extractor: KirExtractor,
) -> None:
    kir = extractor.extract(
        "检查高后备用保护压板确在投入位置", source="ticket"
    )
    assert _state_values(kir) == ["投入位置"]
    assert _targets(kir, TargetKind.DEVICE)[0].suffix == "保护压板"


def test_maintenance_plate_name_is_not_state(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "检查2KLP14PRS-75335kV冉固线保护装置投检修态压板确在投入位置",
        source="ticket",
    )
    assert _state_values(kir) == ["投入位置"]


def test_secondary_is_not_quantity(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "拉开110kV仿香线、#2PT间隔汇控柜PT二次侧0.2级空气开关三只",
        source="ticket",
    )
    assert [(q.number, q.unit) for q in kir.quantities] == [(3, "只")]


def test_coordinated_air_switch_keeps_first_qualifier(
    extractor: KirExtractor,
) -> None:
    kir = extractor.extract(
        "合上汇控柜隔离、接地控制电源空气开关", source="ticket"
    )
    assert [a.value for a in kir.actions] == ["合上"]
    target = _targets(kir, TargetKind.DEVICE, "空气开关")[0]
    assert target.place == "汇控柜"
    assert "隔离" in target.qualifiers
    assert "接地控制电源" in target.qualifiers
    assert kir.residual == ""


def test_operation_item_prefix_is_noise(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "操作项目合上仿河线111间隔汇控柜隔离、接地控制电源空气开关",
        source="ticket",
    )
    assert kir.parse_text.startswith("合上")
    target = _targets(kir, TargetKind.DEVICE, "空气开关")[0]
    assert target.place is not None and target.place.endswith("汇控柜")
    assert kir.residual == ""


def test_operating_task_keeps_voltage_and_interval(
    extractor: KirExtractor,
) -> None:
    kir = extractor.extract(
        "在顺控主机新建#2主变110kV侧102间隔操作任务", source="ticket"
    )
    task = _targets(kir, TargetKind.OBJECT, "操作任务")[0]
    assert task.voltage == "110kv"
    assert task.interval == "#2主变"
    assert task.id is not None and task.id.raw == "102"


def test_busbar_protection_keeps_voltage(extractor: KirExtractor) -> None:
    kir = extractor.extract(
        "停用后台机35kV母线保护分图、电流差动软压板", source="ticket"
    )
    assert any(t.voltage == "35kv" for t in kir.targets)
    assert _targets(kir, TargetKind.DEVICE, "软压板")
