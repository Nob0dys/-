from app.matching import bigram_dice, compare_units, match_line, name_core, score_record


def record(**overrides):
    base = {
        "id": "h1",
        "name": "电子天平",
        "spec": "量程100g，精度0.001g，带防风罩",
        "product_code": "",
        "model": "FA1004",
        "brand": "测试品牌",
        "manufacturer": "测试仪器有限公司",
        "unit": "台",
        "price": 1200,
        "source_file": "历史.xlsx",
        "source_priority": 1,
    }
    base.update(overrides)
    return base


def line(**overrides):
    base = {
        "name": "电子天平",
        "spec": "量程100g，精度0.001g，带防风罩",
        "product_code": "",
        "model": "FA1004",
        "brand": "",
        "manufacturer": "",
        "unit": "台",
    }
    base.update(overrides)
    return base


def test_parameter_weight_selects_correct_same_name_candidate():
    right = record(id="right")
    wrong = record(id="wrong", spec="量程5000g，精度1g，教学用", model="JY-5")
    candidates = match_line(line(), [wrong, right])
    assert candidates[0].record["id"] == "right"
    assert candidates[0].component_scores["参数"] > candidates[1].component_scores["参数"]


def test_convertible_and_incompatible_units():
    converted = compare_units("克", "千克", 100)
    assert converted.status == "convertible"
    assert converted.normalized_price == 0.1
    blocked = compare_units("克", "瓶", 100)
    assert blocked.status == "incompatible"
    assert blocked.warning.startswith("BLOCK:")


def test_package_unit_converts_only_with_explicit_net_content():
    converted = compare_units("克", "瓶", 20, source_spec="每瓶净含量100克")
    assert converted.status == "convertible"
    assert converted.normalized_price == 0.2

    blocked = compare_units("克", "瓶", 20, source_spec="试剂一瓶")
    assert blocked.status == "incompatible"


def test_special_requirement_caps_confidence():
    candidate = score_record(
        line(),
        record(spec="塑料外壳，量程100g，精度0.001g"),
        [{"attribute_name": "材质", "operator": "contains", "value": "不锈钢", "required": True}],
    )
    assert candidate.confidence == "review"
    assert any(item.startswith("BLOCK:") for item in candidate.warnings)


def test_unrelated_product_is_not_reliable():
    candidate = score_record(line(), record(name="冰箱", spec="容积200L", model="BCD-200", unit="台"))
    assert candidate.confidence == "unreliable"


def test_digital_inquiry_downgrades_plain_variant():
    """数显/数字 inquiry must rank the digital variant above a plain one."""
    digital = record(id="digital", name="数显游标卡尺", spec="0～150mm，分辨力0.01mm，液晶显示", price=120)
    plain = record(id="plain", name="游标卡尺", spec="0～150mm，分度值0.02mm", price=30)
    candidates = match_line(
        {"name": "高中物理数显游标卡尺", "spec": "测量范围0mm～150mm，分辨力0.01mm", "unit": "把",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [plain, digital],
    )
    assert candidates[0].record["id"] == "digital"
    assert any("数显" in item for item in candidates[1].warnings) or "规格变体" in "".join(candidates[1].warnings)


def test_capacity_mismatch_downgrades_wrong_capacity():
    """500g 托盘天平 must outrank a 200g candidate on the same line."""
    right = record(id="g500", name="托盘天平", spec="最大称量500g，分度值0.5g", price=55)
    short = record(id="g200", name="托盘天平", spec="最大称量200g，分度值0.2g", price=30)
    candidates = match_line(
        {"name": "高中物理托盘天平", "spec": "测量范围0g～500g，分度值0.5g", "unit": "台",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [short, right],
    )
    assert candidates[0].record["id"] == "g500"
    assert any("量程不符" in item for item in candidates[1].warnings)


def test_pool_merges_exact_name_with_generic_name_candidates():
    """Exact-name match must not block generic-name price sources from joining."""
    exact = record(id="echo", name="高中物理数显游标卡尺", spec="测量范围0mm～150mm，分辨力0.01mm", price=30)
    generic = record(id="generic", name="数显游标卡尺", spec="150mm，0.01mm，液晶显示", price=120)
    candidates = match_line(
        {"name": "高中物理数显游标卡尺", "spec": "测量范围0mm～150mm，分辨力0.01mm", "unit": "把",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [exact, generic],
    )
    ids = [c.record["id"] for c in candidates]
    assert "generic" in ids


def test_name_core_floor_separates_mismatches_from_real_products():
    """泛称/语义变体错配（仿真实验→静电实验箱、条形→蹄形强磁体）不满足自动带价门槛，
    而真实产品（电子起电机→电子起电机）满足，用于阻止自动带错价。"""
    from app.services import name_allows_autoselect
    assert name_allows_autoselect("高中物理仿真实验", "静电实验箱") is False
    assert name_allows_autoselect("高中生物水平电泳槽", "漏斗") is False
    assert name_allows_autoselect("高中物理条形强磁体", "蹄形强磁体") is False
    assert name_allows_autoselect("高中物理洛伦兹力演示器", "摩擦力演示器") is False
    assert name_allows_autoselect("高中生物解剖镊1", "普通手术剪") is False
    assert name_allows_autoselect("高中物理电子起电机", "电子起电机") is True
    assert name_allows_autoselect("高中物理多用电表1", "多用电表") is True
    assert name_allows_autoselect("高中物理数显游标卡尺", "数显游标卡尺") is True
    # 赛特尔价目本的前缀/材质变体（直尺↔钢直尺）放宽允许；其他来源仍拦截
    assert name_allows_autoselect("直尺", "钢直尺", "赛特尔25年.xls") is True
    assert name_allows_autoselect("直尺", "钢直尺", "普教清单.xlsx") is False
    # 一字之差/语义变体（槽码↔钩码）即使赛特尔来源也不允许；
    # 演示器↔实验器 按客户确认视为等价（摩擦力演示器=摩擦力实验器）
    assert name_allows_autoselect("金属槽码", "金属钩码", "赛特尔25年.xls") is False
    assert name_allows_autoselect("阿基米德原理演示器", "阿基米德原理实验器", "赛特尔25年.xls") is True
    assert name_allows_autoselect("演示螺旋测微器", "螺旋测微器", "赛特尔25年.xls") is False
    # 修饰词变体（演示/实验/内能/新型 差异）不允许自动带价
    assert name_allows_autoselect("演示斜面小车", "斜面小车", "赛特尔25年.xls") is False
    assert name_allows_autoselect("新型船闸模型", "船闸模型", "赛特尔25年.xls") is False
    assert name_allows_autoselect("机械能互变演示器", "机械能内能互变演示器", "赛特尔25年.xls") is False
    assert name_allows_autoselect("演示斜面小车", "演示斜面小车", "赛特尔25年.xls") is True
    assert name_allows_autoselect("斜面小车", "斜面小车", "赛特尔25年.xls") is True
    # 后缀超集的功能/型号前缀（图形计算器 ⊃ 计算器）是不同产品，拦截；
    # 前缀超集的组成说明（白卡纸带四方格、双面胶… ⊃ 白卡纸带四方格）是同一产品，放行
    assert name_allows_autoselect("计算器", "图形计算器", "赛特尔25年.xls") is False
    assert (
        name_allows_autoselect(
            "白卡纸(带四方格)", "白卡纸( 带四方格 )、双面胶、线绳、细沙等", "赛特尔25年.xls"
        )
        is True
    )


def test_exact_code_does_not_exclusively_lock_candidate_pool():
    """跨价目本编号体系冲突（预算清单 01013=计算器 vs 赛特尔 01013=图形计算器）
    时，同码记录不得独占候选池——精确同名候选必须参与评分。"""
    code_collision = record(
        id="gcalc",
        name="图形计算器",
        spec="具有常规计算、图象/表格、方程求解、简单程序编制等功能",
        product_code="01013",
        price=600,
    )
    exact = record(
        id="calc",
        name="计算器",
        spec="简易型。8位单行LCD显示、四则运算、开平方、独立储存器",
        product_code="01012",
        price=14,
        unit="个",
    )
    candidates = match_line(
        line(name="计算器", spec="简易型。", product_code="01013", unit="个", model=""),
        [code_collision, exact],
    )
    ids = [c.record["id"] for c in candidates]
    assert "calc" in ids


def test_spec_ranges_extracts_ranges():
    """P0: spec_ranges should extract ranges from various spec formats."""
    from app.matching import spec_ranges
    assert spec_ranges("Φ7～8mm") == {"mm": (7.0, 8.0)}
    assert spec_ranges("φ7mm～8mm") == {"mm": (7.0, 8.0)}
    r = spec_ranges("250mm×180mm×100mm")
    assert r["mm"] == (100.0, 250.0)
    assert r["dims"] == [100.0, 180.0, 250.0]
    assert spec_ranges("-30~50℃") == {"℃": (-30.0, 50.0)}
    assert spec_ranges("-50～40℃") == {"℃": (-50.0, 40.0)}
    # Single value now extracted as (value, value) range
    assert spec_ranges("150mm") == {"mm": (150.0, 150.0)}
    assert spec_ranges("量程100g，精度0.001g") == {"g": (100.0, 100.0)}


def test_spec_features_extracts_new_dimensions():
    """P1: _spec_features should extract shape, material, magnification."""
    from app.matching import _spec_features
    # Shape
    feat = _spec_features("干燥管 U型，φ15mm×150mm")
    assert feat["shape"] == "U型"
    feat = _spec_features("干燥管 单球，150mm")
    assert feat["shape"] == "单球"
    # Material
    feat = _spec_features("试管 高硼硅玻璃材质，试管外径Φ12mm")
    assert feat["material"] == "高硼硅"
    feat = _spec_features("玻璃管 透明钠钙玻璃材质；外径Φ7mm~Φ8mm")
    assert feat["material"] == "钠钙"
    # Magnification
    feat = _spec_features("学生显微镜 200×，单筒")
    assert feat["magnification"] == "200×"
    feat = _spec_features("显微镜 200×10")
    assert feat["magnification"] == "200×10"


def test_parameter_similarity_range_comparison():
    """P0: parameter_similarity should give bonus for overlapping ranges."""
    from app.matching import parameter_similarity
    # Same range, different format
    sim = parameter_similarity("Φ7～8mm", "φ7mm～8mm")
    assert sim >= 0.5  # Should be improved from 0.267
    # Different temperature ranges
    sim = parameter_similarity("-30~50℃", "-50～40℃")
    assert sim >= 0.3  # Should be improved from 0.267
    # Identical 3D dimensions
    sim = parameter_similarity("250mm×180mm×100mm", "250mm×180mm×100mm")
    assert sim >= 0.9
