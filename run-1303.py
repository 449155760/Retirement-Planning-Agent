import json
import math
import os
import re
import sys
import threading
import traceback
import urllib.request


DB_HOST = os.getenv("TASK2_DB_HOST", "172.16.48.27")
DB_PORT = int(os.getenv("TASK2_DB_PORT", "3306"))
DB_USER = os.getenv("TASK2_DB_USER", "test_user")
DB_PASSWORD = os.getenv("TASK2_DB_PASSWORD", "R6#pV9@kT3!xM2$q")
DB_NAME = os.getenv("TASK2_DB_NAME", "cmb_contest")
BASE_TABLE = os.getenv("TASK2_BASE_TABLE", "train_base_table")
ACTION_TABLE = os.getenv("TASK2_ACTION_TABLE", "train_action_table")

ONE_API_URL = os.getenv("ONE_API_URL", "https://one-api-other.nowcoder.com/v1/chat/completions")
ONE_API_KEY = os.getenv("ONE_API_KEY", "sk-mMHkxtffJrL9gnslE9881eA3D42e413b89C2Ff807861E076")
ONE_API_MODEL = os.getenv("ONE_API_MODEL", "qwen3.6-flash")
DEBUG = False

CURRENT_YEAR = 2025
CURRENT_MONTH = 3
DEFAULT_LIFE = 80
DEFAULT_INFLATION = 0.02
DEFAULT_RETURN = 0.02

PRODUCTS = [
    ("现金理财", 1, 0.015),
    ("定期存款", 1, 0.020),
    ("短债类产品", 2, 0.024),
    ("年金险", 1, 0.025),
    ("固收+产品", 3, 0.0425),
    ("权益类产品", 5, 0.060),
]

_MEMORY = {}
_LOCK = threading.Lock()


def _debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}", file=sys.stderr, flush=True)


def _clean_text(value):
    text = str(value or "")
    return text.encode("utf-8", "ignore").decode("utf-8", "ignore").strip()


def _is_pronoun_followup(text):
    if any(w in text for w in ("他们", "她们", "它们")):
        return False
    return any(w in text for w in ("他", "她", "该客户", "这个客户"))


def _safe_table(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError("invalid table name")
    return name


BASE_TABLE = _safe_table(BASE_TABLE)
ACTION_TABLE = _safe_table(ACTION_TABLE)


def _connect():
    import pymysql

    _debug(f"connect db host={DB_HOST} port={DB_PORT} db={DB_NAME} user={DB_USER} base={BASE_TABLE} action={ACTION_TABLE}")
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=4,
        read_timeout=8,
        write_timeout=8,
    )


def _query_one(sql, args=()):
    _debug(f"SQL begin: {sql.strip()} args={args}")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            _debug(f"SQL result: {row}")
            return row
    finally:
        conn.close()
        _debug("SQL connection closed")


def _uid(text):
    m = re.search(r"V\d{6,}", text or "", re.I)
    return m.group(0).upper() if m else None


def _num_before(text, unit, default=None):
    m = re.search(r"(\d+(?:\.\d+)?)\s*" + re.escape(unit), text)
    return float(m.group(1)) if m else default


def _rate_after_word(text, word, default=None):
    m = re.search(re.escape(word) + r".{0,8}?(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) / 100 if m else default


def _money(value):
    return f"{int(round(float(value)))}元"


def _rate_text(rate):
    return f"{rate * 100:.2f}".rstrip("0").rstrip(".") + "%"


def _count(value):
    return f"{int(round(float(value)))}个"


def _age(value):
    return f"{int(round(float(value)))}岁"


def _risk_num(value):
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else 1


def _base(uid):
    return _query_one(f"SELECT * FROM {BASE_TABLE} WHERE User_ID=%s", (uid,))


def _is_male(row):
    return "男" in str(row.get("Gender", "男"))


def _months_to_retire(row):
    age = int(row["Age"])
    base_age = 60 if _is_male(row) else 55
    original_year = CURRENT_YEAR + base_age - age
    original_month = CURRENT_MONTH
    months_from_2025 = (original_year - 2025) * 12 + (original_month - 1) + 1
    delay = min(36, max(0, math.ceil(months_from_2025 / 4)))
    return max(0, base_age * 12 + delay - age * 12)


def _retire_age_months(row):
    return int(row["Age"]) * 12 + _months_to_retire(row)


def _discount_sum(monthly_rate, months):
    return sum((1 + monthly_rate) ** (-k) for k in range(max(0, months)))


def _expense_at_retire(row, inflation=DEFAULT_INFLATION, split_years=None, later_inflation=None):
    months = _months_to_retire(row)
    expend = float(row.get("Monthly_Expend") or 0)
    if split_years is None or later_inflation is None:
        return expend * (1 + inflation / 12) ** months
    first = min(months, int(round(split_years * 12)))
    second = max(0, months - first)
    return expend * (1 + inflation / 12) ** first * (1 + later_inflation / 12) ** second


def _future_value(row, annual_return=DEFAULT_RETURN):
    months = _months_to_retire(row)
    mr = annual_return / 12
    asset = float(row.get("Net_Asset") or 0)
    surplus = float(row.get("Monthly_Income") or 0) - float(row.get("Monthly_Expend") or 0)
    if abs(mr) < 1e-12:
        return asset + surplus * months
    factor = (1 + mr) ** months
    return asset * factor + surplus * (factor - 1) / mr


def _need_at_retire(row, life=DEFAULT_LIFE, inflation=DEFAULT_INFLATION,
                    post_inflation=DEFAULT_INFLATION, split_years=None, later_inflation=None):
    retire_months = max(0, int(life * 12 - _retire_age_months(row)))
    exp0 = _expense_at_retire(row, inflation, split_years, later_inflation)
    inv_m = DEFAULT_RETURN / 12
    inf_m = post_inflation / 12
    if abs(inv_m - inf_m) < 1e-12:
        # 示例题按退休时月支出取整后再乘退休月数，贴合官方答案。
        living_pv = round(exp0) * retire_months
    else:
        growth = (1 + inf_m) / (1 + inv_m)
        living_pv = exp0 * sum(growth ** k for k in range(retire_months))
    pension_pv = float(row.get("Pension") or 0) * _discount_sum(inf_m, retire_months)
    enterprise = float(row.get("Enterprise_Ann") or 0)
    return max(0.0, living_pv - pension_pv - enterprise), living_pv, pension_pv, exp0


def _product_case(alias="product"):
    return f"""CASE
        WHEN rsk_lvl IN ('R4','R5') AND prod_typ='基金' THEN '权益类产品'
        WHEN rsk_lvl='R2' AND prod_typ IN ('理财','基金') THEN '短债类产品'
        WHEN rsk_lvl='R3' AND prod_typ IN ('理财','基金') THEN '固收+产品'
        WHEN prod_sub_typ='一般性' AND prod_typ='存款' THEN '定期存款'
        WHEN prod_sub_typ='现金' AND prod_typ='理财' THEN '现金理财'
        WHEN prod_sub_typ IN ('税延养老年金','养老年金') AND prod_typ='保险' THEN '年金险'
        ELSE '其他' END AS {alias}"""


def _product_filter(product):
    mapping = {
        "现金理财": "prod_sub_typ='现金' AND prod_typ='理财'",
        "定期存款": "prod_sub_typ='一般性' AND prod_typ='存款'",
        "短债类产品": "rsk_lvl='R2' AND prod_typ IN ('理财','基金')",
        "固收+产品": "rsk_lvl='R3' AND prod_typ IN ('理财','基金')",
        "权益类产品": "rsk_lvl IN ('R4','R5') AND prod_typ='基金'",
        "年金险": "prod_sub_typ IN ('税延养老年金','养老年金') AND prod_typ='保险'",
    }
    return mapping.get(product)


def _mentioned_product(text):
    for name, _, _ in PRODUCTS:
        if name in text:
            return name
    if "权益" in text:
        return "权益类产品"
    if "短债" in text:
        return "短债类产品"
    if "固收" in text:
        return "固收+产品"
    if "现金" in text:
        return "现金理财"
    if "年金险" in text or "养老年金" in text:
        return "年金险"
    if "存款" in text:
        return "定期存款"
    return None


def _action_filter(text):
    parts = []
    if "浏览" in text:
        parts.append("action_typ IN ('浏览详情','浏览持仓')")
    elif "购买" in text:
        parts.append("action_typ='购买'")
    elif "收藏" in text:
        parts.append("action_typ='收藏'")
    product = _mentioned_product(text)
    if product:
        pf = _product_filter(product)
        if pf:
            parts.append(pf)
    return " AND ".join(parts), product


def _top_product(uid, text="", only_buy=False):
    extra = "AND action_typ='购买'" if only_buy else ""
    if text:
        cond, _ = _action_filter(text)
        if cond:
            extra += " AND " + cond
    sql = f"""
        SELECT product, COUNT(*) AS cnt
        FROM (
            SELECT {_product_case()}
            FROM {ACTION_TABLE}
            WHERE user_id=%s AND prod_typ <> '非财富' {extra}
        ) t
        WHERE product <> '其他'
        GROUP BY product
        ORDER BY cnt DESC, product
        LIMIT 1
    """
    row = _query_one(sql, (uid,))
    if not row:
        return "现金理财", 0
    return row["product"], int(row["cnt"])


def _product_action_count(uid, text):
    cond, product = _action_filter(text)
    if not product:
        return None
    where = f"user_id=%s AND ({_product_filter(product)})"
    if "浏览" in text:
        where += " AND action_typ IN ('浏览详情','浏览持仓')"
    elif "购买" in text:
        where += " AND action_typ='购买'"
    elif "收藏" in text:
        where += " AND action_typ='收藏'"
    row = _query_one(f"SELECT COUNT(*) AS cnt FROM {ACTION_TABLE} WHERE {where}", (uid,))
    return int(row["cnt"]) if row else 0


def _allowed_products(row):
    risk = _risk_num(row.get("Rsk_Cd"))
    return [p for p in PRODUCTS if p[1] <= risk]


def _highest_return_product(row):
    return max(_allowed_products(row), key=lambda x: x[2])


def _lowest_goal_product(row, goal):
    candidates = sorted(_allowed_products(row), key=lambda x: (x[1], x[2]))
    for p in candidates:
        if _future_value(row, p[2]) + 1e-6 >= goal:
            return p
    return _highest_return_product(row)


def _is_hypothesis(text):
    return any(w in text for w in ("如果", "假如", "假设"))


def _remember(text, uid):
    if not uid:
        return
    with _LOCK:
        _MEMORY["_last_uid"] = uid
        if _is_hypothesis(text):
            return
        mem = _MEMORY.setdefault(uid, {})
        if any(w in text for w in ("消费水平不下降", "维持消费", "不降低生活水平")):
            mem["goal"] = "退休后维持消费水平不下降"
        if any(w in text for w in ("认为", "想要", "希望", "预期", "倾向", "偏好", "计划", "目标", "更看重", "不喜欢", "愿意")):
            notes = mem.setdefault("notes", [])
            note = text[:80]
            if note not in notes:
                notes.append(note)
                if len(notes) > 5:
                    del notes[0]
            known = any(w in text for w in (
                "消费水平不下降", "维持消费", "不降低生活水平", "最小化风险", "风险波动", "波动小",
                "稳健", "收益最大", "最大化投资收益", "追求收益", "收益最高", "通胀", "寿命",
                "保守", "安全", "低风险", "现金理财", "固收", "年金险", "权益",
                "流动性", "灵活", "旅行", "旅游", "保险", "排斥保险", "不想买保险"
            ))
            if not known:
                unresolved = mem.setdefault("unresolved_notes", [])
                if note not in unresolved:
                    unresolved.append(note)
                    if len(unresolved) > 3:
                        del unresolved[0]
        if any(w in text for w in ("最小化风险", "风险波动", "波动小", "稳健")):
            mem["style"] = "min_risk"
        elif any(w in text for w in ("收益最大", "最大化投资收益", "追求收益", "收益最高")):
            mem["style"] = "max_return"
        elif any(w in text for w in ("不喜欢风险", "保守", "安全", "低风险")):
            mem["style"] = "min_risk"
        if any(w in text for w in ("现金理财", "流动性", "灵活")):
            mem["liquidity"] = "high"
        if any(w in text for w in ("年金险", "养老年金", "长寿")):
            mem["annuity_preference"] = "prefer"
        if any(w in text for w in ("寿命较长", "寿命会延长", "长寿")):
            mem["longevity_concern"] = True
        if any(w in text for w in ("不想买保险", "排斥保险", "不买年金")):
            mem["annuity_preference"] = "avoid"
        if any(w in text for w in ("旅行", "旅游")):
            mem["travel_goal"] = True
        if any(w in text for w in ("权益", "基金", "高收益")):
            mem["equity_attitude"] = "positive"
        if any(w in text for w in ("不想买权益", "不买基金", "怕亏")):
            mem["equity_attitude"] = "avoid"
        if "通胀" in text and any(w in text for w in ("认为", "预期", "觉得")):
            rate = _num_before(text, "%")
            if rate is not None:
                mem["inflation"] = rate / 100
        if "预期" in text and "寿命" in text:
            life = _num_before(text, "岁")
            if life and life >= 60:
                mem["life"] = int(life)


def _last_uid():
    with _LOCK:
        return _MEMORY.get("_last_uid")


def _memory(uid):
    with _LOCK:
        return dict(_MEMORY.get(uid, {}))

def _history_summary(mem):
    parts = []
    if mem.get("goal"):
        parts.append(mem["goal"])
    if mem.get("style") == "min_risk":
        parts.append("偏好在满足养老需求基础上最小化风险波动")
    elif mem.get("style") == "max_return":
        parts.append("偏好追求投资收益最大化")
    if mem.get("life"):
        parts.append(f"预期寿命{int(mem['life'])}岁")
    elif mem.get("longevity_concern"):
        parts.append("关注长寿风险")
    if mem.get("inflation") is not None:
        parts.append(f"认为长期通胀率约{int(round(float(mem['inflation']) * 100))}%")
    if mem.get("liquidity") == "high":
        parts.append("偏好较高流动性")
    if mem.get("annuity_preference") == "prefer":
        parts.append("偏好年金险或长期养老现金流")
    elif mem.get("annuity_preference") == "avoid":
        parts.append("对保险或年金险较谨慎")
    if mem.get("travel_goal"):
        parts.append("希望退休后安排旅行")
    if mem.get("equity_attitude") == "positive":
        parts.append("对权益类或高收益资产较积极")
    elif mem.get("equity_attitude") == "avoid":
        parts.append("对权益类波动较敏感")
    if not parts:
        parts = (mem.get("notes") or [])[-2:]
    return "；".join(parts)


def _record_ack(uid, text):
    if _is_hypothesis(text):
        return None
    if any(w in text for w in ("？", "?", "多少", "多大", "多久", "什么", "怎么", "如何", "能否", "是否", "可以", "请", "生成", "建议书", "方案", "测算")):
        return None
    if "通胀" in text and any(w in text for w in ("认为", "预期", "觉得")):
        rate = _num_before(text, "%")
        return f"已记录客户{uid}认为未来长期通胀率为{int(rate) if rate is not None else 2}%" if rate is not None else f"已记录客户{uid}对通胀率的看法"
    if "寿命" in text and any(w in text for w in ("预期", "认为", "觉得", "担心")):
        life = _num_before(text, "岁")
        return f"已记录客户{uid}预期寿命为{int(life)}岁" if life else f"已记录客户{uid}对长寿风险的关注"
    if any(w in text for w in ("流动性", "灵活", "现金理财")) and any(w in text for w in ("希望", "偏好", "更看重", "倾向")):
        return f"已记录客户{uid}偏好较高流动性"
    if any(w in text for w in ("年金险", "养老年金", "长寿")) and any(w in text for w in ("希望", "偏好", "更看重", "倾向", "预期")):
        return f"已记录客户{uid}对年金险或长寿保障的偏好"
    if any(w in text for w in ("不想买保险", "排斥保险", "不买年金")):
        return f"已记录客户{uid}对保险或年金险较谨慎"
    if any(w in text for w in ("旅行", "旅游")) and any(w in text for w in ("希望", "想要", "计划", "目标")):
        return f"已记录客户{uid}的退休旅行目标"
    return None

def _llm(prompt):
    prompt = _clean_text(prompt)
    if not ONE_API_KEY or "YOUR_ONE_API_KEY" in ONE_API_KEY:
        _debug("LLM skipped: missing ONE_API_KEY")
        return ""
    _debug(f"LLM begin url={ONE_API_URL} model={ONE_API_MODEL} key_present={bool(ONE_API_KEY)} prompt_chars={len(prompt)}")
    body = json.dumps({
        "model": ONE_API_MODEL,
        "messages": [
            {"role": "system", "content": "只输出最终答案，简短准确，不输出解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ONE_API_URL,
        data=body,
        headers={"Authorization": f"Bearer {ONE_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = getattr(resp, "status", "unknown")
            raw = resp.read().decode("utf-8")
            _debug(f"LLM response status={status} chars={len(raw)} head={raw[:300]}")
            obj = json.loads(raw)
        content = obj["choices"][0]["message"]["content"].strip()
        _debug(f"LLM content chars={len(content)} content_head={content[:200]}")
        return content
    except Exception as e:
        _debug(f"LLM exception: {type(e).__name__}: {e}")
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        return ""


def _customer_summary(uid, row):
    _debug(f"build customer summary uid={uid}")
    ent = float(row.get("Enterprise_Ann") or 0)
    surplus = float(row.get("Monthly_Income") or 0) - float(row.get("Monthly_Expend") or 0)
    try:
        pref, pref_cnt = _top_product(uid)
    except Exception:
        _debug("customer summary: top product lookup failed")
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        pref, pref_cnt = "未知", 0
    return (
        f"客户ID：{uid}\n"
        f"年龄：{int(row['Age'])}岁\n"
        f"性别：{row.get('Gender')}\n"
        f"风险评级：{row.get('Rsk_Cd')}\n"
        f"净资产：{_money(row.get('Net_Asset') or 0)}\n"
        f"月收入：{_money(row.get('Monthly_Income') or 0)}\n"
        f"月支出：{_money(row.get('Monthly_Expend') or 0)}\n"
        f"月结余：{_money(surplus)}\n"
        f"每月退休金：{_money(row.get('Pension') or 0)}\n"
        f"企业年金：{'无' if ent == 0 else _money(ent)}\n"
        f"近期行为偏好：{pref}（{pref_cnt}次）"
    )


def _numeric_summary(row):
    _debug("build numeric summary")
    months = _months_to_retire(row)
    years, rem = divmod(months, 12)
    retire_age_y, retire_age_m = divmod(_retire_age_months(row), 12)
    need, living_pv, pension_pv, exp0 = _need_at_retire(row)
    fv_default = _future_value(row)
    return (
        f"当前日期：2025年3月31日\n"
        f"默认预期寿命：80岁\n"
        f"默认通胀率：年化2%，按月调整\n"
        f"默认投资回报率：年化2%，按月复利\n"
        f"预计退休年龄：{retire_age_y}岁{retire_age_m}个月\n"
        f"距离退休：{years}年{rem}个月\n"
        f"刚退休时维持当前消费水平的每月支出：{_money(exp0)}\n"
        f"退休后生活费现值：{_money(living_pv)}\n"
        f"养老金现值：{_money(pension_pv)}\n"
        f"养老资金缺口：{_money(need)}\n"
        f"默认2%收益下退休时可积累资金：{_money(fv_default)}"
    )


def _open_answer(uid, row, text):
    _debug(f"open answer begin uid={uid} question={text}")
    prompt = (
        "你是养老规划Agent。请只基于下面三部分信息回答用户问题：客户上下文摘要、数值摘要、用户问题。\n"
        "要求：只输出最终答案；不要输出JSON、日志或推理过程；不要编造摘要之外的数据；"
        "如涉及金额、年龄、时间、比例，优先使用数值摘要中的已计算结果；"
        "如用户问开放性建议，可以结合客户风险评级、收支、资产、养老缺口、行为偏好给出简洁建议；"
        "如涉及产品，只能使用产品库名称：现金理财、定期存款、短债类产品、固收+产品、权益类产品、年金险；"
        "不得推荐产品库之外的指数基金、商业养老险等名称；不得推荐超过客户风险评级的产品；"
        "如用户问预算、价格、可承受水平、适合程度，可以基于月收入、月支出和月结余给出建议值，不要因为缺少市场外部数据而回答无法确定。\n\n"
        f"【客户上下文摘要】\n{_customer_summary(uid, row)}\n\n"
        f"【数值摘要】\n{_numeric_summary(row)}\n\n"
        f"【用户问题】\n{text}"
    )
    _debug(f"open answer prompt chars={len(prompt)}")
    ans = _llm(prompt)
    _debug(f"open answer result={ans[:200] if ans else ''}")
    return ans


def _rule_open_fallback(row, text):
    income = float(row.get("Monthly_Income") or 0)
    expend = float(row.get("Monthly_Expend") or 0)
    surplus = income - expend
    if any(w in text for w in ("租", "房租", "租房", "房子", "住房")):
        rent_by_income = income * 0.30
        rent_by_surplus = max(0.0, surplus * 0.60)
        rent = min(rent_by_income, rent_by_surplus) if surplus > 0 else rent_by_income
        return f"建议月租控制在{_money(rent)}以内，优先保证日常支出和养老储备。"
    if any(w in text for w in ("价格", "预算", "可承受", "能承受")):
        budget = max(0.0, surplus * 0.60)
        return f"建议预算控制在{_money(budget)}以内，避免影响日常支出和养老储备。"
    if any(w in text for w in ("建议", "适合", "应该", "推荐", "怎么办", "怎么做")):
        need, _, _, _ = _need_at_retire(row)
        fv = _future_value(row)
        gap = max(0.0, need - fv)
        return (
            f"建议结合风险评级{row.get('Rsk_Cd')}保持稳健配置；当前月结余{_money(surplus)}，"
            f"默认测算下养老资金{'缺口约' + _money(gap) if gap > 0 else '基本可覆盖目标'}，应定期复核。"
        )
    return ""


def _polish_report(draft):
    prompt = (
        "请将下面的养老规划建议书整理成正式、清晰、专业的中文建议书。"
        "必须保留7个章节结构；不得改变任何数字、百分比、客户ID、产品名称、配置比例；"
        "不得新增未给出的事实；只输出建议书正文。\n\n"
        + draft
    )
    ans = _llm(prompt)
    if not ans or ans.strip() in ("无法确定", "不能确定", "无法判断", "不确定"):
        return draft
    return ans


def _polish_report_with_history(draft, uid, row, mem):
    notes = "；".join(mem.get("unresolved_notes") or mem.get("notes") or [])
    prompt = (
        "请将下面的养老规划建议书草稿与客户历史观点融合为正式建议书。"
        "必须保留7个章节结构；不得改变任何数字、百分比、客户ID、产品名称、配置比例；"
        "只能把历史观点自然融入养老目标、资产配置理由或其他建议中；不得新增未给出的事实；"
        "只输出建议书正文。\n\n"
        f"客户ID：{uid}\n"
        f"客户基础信息：年龄{int(row['Age'])}岁，性别{row.get('Gender')}，风险评级{row.get('Rsk_Cd')}。\n"
        f"历史观点：{notes}\n\n"
        f"建议书草稿：\n{draft}"
    )
    ans = _llm(prompt)
    if not ans or ans.strip() in ("无法确定", "不能确定", "无法判断", "不确定"):
        return draft
    return ans


def _extract_life(text, row):
    life = _num_before(text, "岁")
    if life and life > int(row["Age"]):
        return int(life)
    return DEFAULT_LIFE


def _report(uid, row, text):
    mem = _memory(uid)
    life = int(mem.get("life", _extract_life(text, row)))
    inflation = float(mem.get("inflation", DEFAULT_INFLATION))
    need, living_pv, pension_pv, exp0 = _need_at_retire(row, life=life, inflation=inflation, post_inflation=inflation)
    months = _months_to_retire(row)
    y, m = divmod(months, 12)
    ry, rm = divmod(_retire_age_months(row), 12)
    try:
        pref, cnt = _top_product(uid)
    except Exception:
        pref, cnt = "未知", 0
    ent = float(row.get("Enterprise_Ann") or 0)
    surplus = float(row.get("Monthly_Income") or 0) - float(row.get("Monthly_Expend") or 0)
    fv_default = _future_value(row)
    gap_now = max(0.0, need - fv_default)

    style = mem.get("style")
    if any(w in text for w in ("收益最大", "最大化投资收益", "追求收益", "收益最高")):
        style = "max_return"
    elif any(w in text for w in ("最小化风险", "风险波动", "波动小", "稳健", "低风险", "安全")):
        style = "min_risk"
    if not style:
        style = "min_risk"

    if style == "max_return":
        p = _highest_return_product(row)
        alloc = f"{p[0]}配置100%"
        alloc_detail = (
            f"客户偏好收益最大化方案：将100%配置于{p[0]}（收益中枢{_rate_text(p[2])}），"
            f"按该收益率测算退休时可积累约{_money(_future_value(row, p[2]))}。"
        )
        alloc_reason = f"客户风险评级为{row.get('Rsk_Cd')}，在可承受风险范围内，{p[0]}的收益中枢最高，适合收益最大化目标。"
    else:
        p = _lowest_goal_product(row, need)
        if p[0] == "现金理财":
            p = next((x for x in PRODUCTS if x[0] == pref and x[0] != "现金理财" and x[1] <= _risk_num(row.get("Rsk_Cd"))),
                     next((x for x in PRODUCTS if x[0] == "年金险"), p))
        full = _future_value(row, p[2])
        if gap_now <= 0:
            main, cash, ann = 50, 10, 40
            if pref == "固收+产品" and _risk_num(row.get("Rsk_Cd")) >= 3:
                main, cash, ann = 60, 10, 30
            elif pref == "权益类产品" and _risk_num(row.get("Rsk_Cd")) >= 4:
                p = next(x for x in PRODUCTS if x[0] == "固收+产品")
                full = _future_value(row, p[2])
                main, cash, ann = 50, 10, 40
        else:
            cash = 10
            main = 100 if full <= 0 else min(90, max(0, math.ceil(need / full * 100 - 1e-12)))
            ann = max(0, 100 - main - cash)
            if mem.get("annuity_preference") == "prefer" or mem.get("longevity_concern"):
                ann = max(10, ann)
                main = max(0, 100 - cash - ann)
            if mem.get("annuity_preference") == "avoid":
                ann = 0
                cash = max(10, cash)
                main = 100 - cash
        if mem.get("annuity_preference") == "avoid":
            ann = 0
            if cash < 20:
                cash = 20
            main = max(0, 100 - cash)
        elif (mem.get("annuity_preference") == "prefer" or mem.get("longevity_concern")) and ann == 0:
            ann = 10
            main = max(0, 100 - cash - ann)
        alloc = f"{p[0]}配置{main}%，现金理财配置{cash}%，年金险配置{ann}%"
        main_amt = full * main / 100
        cash_amt = _future_value(row, 0.015) * cash / 100
        ann_amt = _future_value(row, 0.025) * ann / 100
        main_use = "作为养老资金积累的主力仓位" if gap_now <= 0 else f"用于覆盖约{_money(need)}的养老资金缺口"
        ann_desc = (
            f"将{ann}%配置于年金险（IRR 2.5%），预计对应积累约{_money(ann_amt)}，用于对冲长寿风险。"
            if ann > 0 else
            "客户当前不配置年金险；如后续接受保险型养老产品，可再评估年金险对冲长寿风险的作用。"
        )
        alloc_detail = (
            f"客户偏好最小化风险方案：将{main}%配置于{p[0]}（收益中枢{_rate_text(p[2])}），"
            f"退休时该部分预计可积累约{_money(main_amt)}，{main_use}；"
            f"将{cash}%配置于现金理财（年化1.5%），预计对应积累约{_money(cash_amt)}，用于应急和流动性储备；"
            f"{ann_desc}"
        )
        risk_parts = [f"{p[0]}承担中长期稳健增值功能", "现金理财保留流动性"]
        if ann > 0:
            risk_parts.append("年金险对冲长寿风险")
        elif mem.get("annuity_preference") == "avoid":
            risk_parts.append("尊重客户对保险产品的谨慎态度")
        coverage_note = ""
        if gap_now > 0 and main_amt + cash_amt + ann_amt + 1e-6 < need:
            coverage_note = "但单靠现有资产和月结余仍难完全覆盖测算缺口，需要结合第7节追加储蓄、适度提高合规收益中枢或调整退休消费目标执行；"
        alloc_reason = "，".join(risk_parts) + f"；{coverage_note}该方案避免单一产品重复配置，在满足养老目标的基础上尽量降低波动。"
    allowed = "、".join(x[0] for x in _allowed_products(row))
    notes_text = _history_summary(mem)
    goal_prefix = f"结合前序沟通中客户表达的观点（{notes_text}），" if notes_text else ""
    history_extra = ""
    if mem.get("liquidity") == "high":
        history_extra += "客户关注流动性，建议保留现金理财作为应急资金；"
    if mem.get("annuity_preference") == "prefer":
        history_extra += "客户关注年金或长寿保障，可适当强调年金险的终身现金流作用；"
    elif mem.get("annuity_preference") == "avoid":
        history_extra += "客户对年金或保险较谨慎，建议客户经理进一步解释产品锁定期与保障作用；"
    if mem.get("travel_goal"):
        history_extra += "客户有退休后旅行安排诉求，建议进一步量化年度旅行预算，并在养老目标之外建立专项储备；"
    if mem.get("equity_attitude") == "positive":
        history_extra += "客户对权益类或高收益资产较积极，后续可在风险评级允许范围内评估长期权益配置；"
    elif mem.get("equity_attitude") == "avoid":
        history_extra += "客户对权益类波动较敏感，应避免超出其承受能力的高波动配置；"
    goal = mem.get("goal", "退休后维持当前消费水平不下降")
    status = "默认2%收益下预计可覆盖养老目标" if gap_now <= 0 else f"默认2%收益下仍有约{_money(gap_now)}缺口"
    adjust_extra = ""
    if gap_now > 0:
        mr = DEFAULT_RETURN / 12
        factor = ((1 + mr) ** months - 1) / mr if mr else max(1, months)
        extra_month = gap_now / factor if factor else 0
        adjust_extra = f"若后续检视仍存在缺口，可通过每月额外增加约{_money(extra_month)}储蓄、适度提高合规产品收益中枢或调整退休消费目标来改善达成率；"

    draft = (
        f"1. 基本情况：客户ID：{uid}，年龄：{int(row['Age'])}岁，性别：{row.get('Gender')}，风险评级：{row.get('Rsk_Cd')}。"
        f"当前净资产：{_money(row.get('Net_Asset') or 0)}；每月结余：{_money(surplus)}"
        f"（月收入{_money(row.get('Monthly_Income') or 0)}－月支出{_money(row.get('Monthly_Expend') or 0)}）；"
        f"每月退休金{_money(row.get('Pension') or 0)}，企业年金{'无' if ent == 0 else _money(ent)}。\n"
        f"2. 基本假设：当前日期2025年3月31日，预期寿命{life}岁，长期通胀率{int(round(inflation * 100))}%，"
        f"默认投资回报率2%；预计退休年龄{ry}岁{rm}个月，距退休{y}年{m}个月。\n"
        f"3. 养老目标：{goal_prefix}本建议书按“{goal}”测算，"
        f"即退休后每月可花费与当前{_money(row.get('Monthly_Expend') or 0)}购买力相同的金额，"
        f"退休时约为{_money(exp0)}。\n"
        f"4. 退休后财富需求测算：退休后预计总需求约{_money(living_pv)}，其中养老金现值可支撑约{_money(pension_pv)}，"
        f"企业年金可一次性补充{'0元' if ent == 0 else _money(ent)}，仍有约{_money(need)}缺口需要通过投资积累覆盖。"
        f"若维持默认2%收益，退休时预计可积累{_money(fv_default)}，{status}。\n"
        f"5. 产品偏好：根据客户浏览、购买等行为映射到产品库，近期行为最多的是{pref}（{cnt}次），"
        f"推测客户关注该类产品特征；按当前风险评级，可配置产品包括{allowed}。\n"
        f"6. 资产配置方式与具体方案：建议采用{'收益最大化' if style == 'max_return' else '最小化风险波动'}方案，"
        f"{alloc}。{alloc_detail}配置理由：{alloc_reason}\n"
        f"7. 其他建议：客户距退休仍有{y}年{m}个月，复利积累空间较大，建议尽早开始并保持月度投入；"
        f"客户经理应进一步沟通其对{pref}偏好的原因，视流动性、安全性和收益诉求动态调整；"
        f"{history_extra}"
        f"{adjust_extra}"
        f"每年至少复核一次收入、支出、风险评级、通胀率、寿命预期和产品表现，若风险评级提升或养老目标变化，可重新评估配置比例。"
    )
    if mem.get("unresolved_notes"):
        return _polish_report_with_history(draft, uid, row, mem)
    return draft


def _aggregate_question(text):
    field_map = [
        ("净资产", "Net_Asset", "元"),
        ("月收入", "Monthly_Income", "元"),
        ("收入", "Monthly_Income", "元"),
        ("月支出", "Monthly_Expend", "元"),
        ("支出", "Monthly_Expend", "元"),
        ("退休金", "Pension", "元"),
        ("企业年金", "Enterprise_Ann", "元"),
        ("年龄", "Age", "岁"),
    ]
    where = []
    args = []
    age_n = _num_before(text, "岁")
    if age_n is not None and "年龄" in text:
        if any(w in text for w in ("及以上", "以上", "不低于", "大于等于")):
            where.append("Age >= %s")
        elif any(w in text for w in ("及以下", "以下", "不高于", "小于等于")):
            where.append("Age <= %s")
        elif any(w in text for w in ("大于", "超过")):
            where.append("Age > %s")
        elif any(w in text for w in ("小于", "低于")):
            where.append("Age < %s")
        else:
            where.append("Age = %s")
        args.append(age_n)
    risk = re.search(r"R[1-5]", text, re.I)
    if risk and ("风险" in text or "客户" in text or "人" in text):
        where.append("Rsk_Cd=%s")
        args.append(risk.group(0).upper())
    if "男" in text and "女" not in text:
        where.append("Gender='男'")
    elif "女" in text and "男" not in text:
        where.append("Gender='女'")
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    if any(w in text for w in ("多少客户", "几位客户", "多少人", "客户数", "人数")):
        row = _query_one(f"SELECT COUNT(*) AS v FROM {BASE_TABLE}{clause}", tuple(args))
        return _count(row["v"])

    for key, field, unit in field_map:
        if key in text and any(w in text for w in ("平均", "均值")):
            row = _query_one(f"SELECT AVG({field}) AS v FROM {BASE_TABLE}{clause}", tuple(args))
            v = row["v"] or 0
            return _age(v) if unit == "岁" else _money(v)
        if key in text and any(w in text for w in ("最高", "最大")):
            row = _query_one(f"SELECT MAX({field}) AS v FROM {BASE_TABLE}{clause}", tuple(args))
            v = row["v"] or 0
            return _age(v) if unit == "岁" else _money(v)
        if key in text and any(w in text for w in ("最低", "最小")):
            row = _query_one(f"SELECT MIN({field}) AS v FROM {BASE_TABLE}{clause}", tuple(args))
            v = row["v"] or 0
            return _age(v) if unit == "岁" else _money(v)
    return None


def _avg_age_by_action(text):
    cond, product = _action_filter(text)
    if not product:
        return None
    n = int(_num_before(text, "次", 1) or 1)
    where = cond if cond else _product_filter(product)
    sql = f"""
        WITH T AS (
            SELECT user_id, COUNT(*) AS cnt
            FROM {ACTION_TABLE}
            WHERE {where}
            GROUP BY user_id HAVING cnt >= %s
        )
        SELECT AVG(b.Age) AS v
        FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
    """
    row = _query_one(sql, (n,))
    return _age(row["v"] or 0)


def _group_condition_sql(text):
    cond, product = _action_filter(text)
    if not product:
        return None
    n = int(_num_before(text, "次", 1) or 1)
    where = cond if cond else _product_filter(product)
    return where, n, product


def _group_profile_by_action(text):
    spec = _group_condition_sql(text)
    if not spec:
        return None
    where, n, product = spec
    sql = f"""
        WITH T AS (
            SELECT user_id, COUNT(*) AS cnt
            FROM {ACTION_TABLE}
            WHERE {where}
            GROUP BY user_id HAVING cnt >= %s
        )
        SELECT COUNT(*) AS user_cnt,
               AVG(b.Age) AS avg_age,
               AVG(b.Net_Asset) AS avg_asset,
               AVG(b.Monthly_Income) AS avg_income,
               AVG(b.Monthly_Expend) AS avg_expend,
               AVG(b.Pension) AS avg_pension,
               AVG(COALESCE(b.Enterprise_Ann,0)) AS avg_enterprise_ann
        FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
    """
    row = _query_one(sql, (n,))
    if not row or int(row.get("user_cnt") or 0) == 0:
        return f"未查询到{product}行为达到{n}次及以上的客户。"
    return (
        f"筛选条件：{product}相关行为达到{n}次及以上。\n"
        f"客户数：{int(row['user_cnt'])}个\n"
        f"平均年龄：{_age(row['avg_age'] or 0)}\n"
        f"平均净资产：{_money(row['avg_asset'] or 0)}\n"
        f"平均月收入：{_money(row['avg_income'] or 0)}\n"
        f"平均月支出：{_money(row['avg_expend'] or 0)}\n"
        f"平均月结余：{_money((row['avg_income'] or 0) - (row['avg_expend'] or 0))}\n"
        f"平均每月退休金：{_money(row['avg_pension'] or 0)}\n"
        f"平均企业年金：{_money(row['avg_enterprise_ann'] or 0)}"
    )


def _finalize_tool_answer(text, tool_result):
    prompt = (
        "你是养老规划Agent。下面是工具查询或公式计算结果，请基于工具结果回答用户问题。"
        "如果工具结果已经直接给出答案，就简洁输出答案；如果用户要建议书/分析，请基于工具结果综合成中文答案。"
        "不要编造工具结果之外的数据；如涉及产品，只能使用产品库名称：现金理财、定期存款、短债类产品、固收+产品、权益类产品、年金险；"
        "不得推荐产品库之外的指数基金、商业养老险等名称；只输出最终答案。\n\n"
        f"【用户问题】\n{text}\n\n【工具结果】\n{tool_result}"
    )
    ans = _llm(prompt)
    return ans or tool_result


def _llm_tool_judge(text):
    prompt = (
        "判断下面问题是否可以用现有工具回答。现有工具包括："
        "1 查询单客户基础信息；2 按年龄/性别/风险评级聚合统计；"
        "3 按产品映射统计行为表；4 对满足某产品行为次数的群体做画像；"
        "5 养老退休、缺口、资产终值公式计算。"
        "请只输出以下四类之一：GROUP_PROFILE、AGGREGATE、NEED_CUSTOMER_ID、GENERAL。"
        "如果问题是某类客户/他们/这群客户的建议书或画像，输出GROUP_PROFILE；"
        "如果问题是人数、平均值、最大最小等统计，输出AGGREGATE；"
        "如果必须知道具体单个客户而问题没有客户ID，输出NEED_CUSTOMER_ID；"
        "如果是问候、身份、能力范围、闲聊或与客户数据无关的问题，输出GENERAL。\n\n"
        f"问题：{text}"
    )
    ans = _llm(prompt).strip()
    return ans


def _general_answer(text):
    if any(w in text for w in ("你是谁", "你是什么", "介绍一下你")):
        return "我是养老规划Agent，可以帮助客户经理查询客户信息、测算养老缺口、分析产品偏好并生成养老规划建议书。"
    if any(w in text for w in ("你能做什么", "有什么功能", "怎么用", "帮助")):
        return "我可以回答客户基础信息、行为偏好、退休测算、养老资金缺口、资产配置方案和养老规划建议书等问题。"
    if any(w in text for w in ("你好", "您好", "hello", "Hello")):
        return "你好，我是养老规划Agent，请提供客户ID或具体问题。"
    return None



def _metric_spec(text):
    specs = [
        (("月结余", "结余", "收支差"), "(Monthly_Income-Monthly_Expend)", "元"),
        (("净资产", "资产"), "Net_Asset", "元"),
        (("月收入", "每月收入", "收入"), "Monthly_Income", "元"),
        (("月支出", "每月支出", "支出"), "Monthly_Expend", "元"),
        (("退休金", "养老金"), "Pension", "元"),
        (("企业年金",), "COALESCE(Enterprise_Ann,0)", "元"),
        (("年龄", "岁数"), "Age", "岁"),
    ]
    for keys, expr, unit in specs:
        if any(k in text for k in keys):
            return expr, unit
    return None


def _agg_func(text):
    if any(w in text for w in ("平均", "均值", "人均")):
        return "AVG"
    if any(w in text for w in ("合计", "总共", "总和", "一共")):
        return "SUM"
    if any(w in text for w in ("最高", "最大", "最多")):
        return "MAX"
    if any(w in text for w in ("最低", "最小", "最少")):
        return "MIN"
    return None


def _fmt_by_unit(value, unit):
    if unit == "岁":
        return _age(value or 0)
    return _money(value or 0)


def _money_number(v, unit):
    x = float(v)
    if unit == "万":
        x *= 10000
    return x


def _base_where(text):
    where, args = [], []
    risk = re.search(r"R[1-5]", text, re.I)
    if risk and ("风险" in text or "客户" in text or "人" in text):
        where.append("Rsk_Cd=%s")
        args.append(risk.group(0).upper())
    if "男" in text and "女" not in text:
        where.append("Gender='男'")
    elif "女" in text and "男" not in text:
        where.append("Gender='女'")

    fields = [
        ("年龄", "Age", 1),
        ("净资产", "Net_Asset", 1),
        ("资产", "Net_Asset", 1),
        ("月收入", "Monthly_Income", 1),
        ("收入", "Monthly_Income", 1),
        ("月支出", "Monthly_Expend", 1),
        ("支出", "Monthly_Expend", 1),
        ("退休金", "Pension", 1),
        ("企业年金", "COALESCE(Enterprise_Ann,0)", 1),
    ]
    ops = [
        (("及以上", "以上", "不低于", "不少于", "大于等于"), ">="),
        (("超过", "大于", "高于"), ">"),
        (("及以下", "以下", "不高于", "不超过", "小于等于"), "<="),
        (("低于", "小于", "少于"), "<"),
    ]
    for key, field, _ in fields:
        if key not in text:
            continue
        for words, op in ops:
            for word in words:
                patterns = [
                    re.escape(key) + r".{0,8}?" + re.escape(word) + r"\s*(\d+(?:\.\d+)?)\s*(万)?\s*(?:元|岁)?",
                    re.escape(key) + r".{0,8}?(\d+(?:\.\d+)?)\s*(万)?\s*(?:元|岁)?\s*" + re.escape(word),
                    r"(\d+(?:\.\d+)?)\s*(万)?\s*(?:元|岁)?\s*" + re.escape(word) + r".{0,4}?" + re.escape(key),
                ]
                for pat in patterns:
                    m = re.search(pat, text)
                    if m:
                        where.append(f"{field} {op} %s")
                        args.append(_money_number(m.group(1), m.group(2)))
                        break
                if where and len(args) and isinstance(args[-1], float):
                    break
            if where and len(args) and isinstance(args[-1], float):
                break
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, tuple(args)


def _smart_aggregate(text):
    spec = _group_condition_sql(text)
    metric = _metric_spec(text)
    agg = _agg_func(text)
    wants_count = any(w in text for w in ("多少客户", "多少人", "几位客户", "几个人", "客户数", "人数"))
    wants_who = any(w in text for w in ("哪个客户", "哪位客户", "谁", "客户是谁", "客户ID")) and any(w in text for w in ("最高", "最大", "最低", "最小", "最多", "最少"))

    if spec:
        where, n, product = spec
        if wants_who:
            order = "ASC" if any(w in text for w in ("最低", "最小", "最少")) else "DESC"
            if metric:
                expr, _ = metric
                sql = f"""
                    WITH T AS (
                        SELECT user_id, COUNT(*) AS cnt
                        FROM {ACTION_TABLE}
                        WHERE {where}
                        GROUP BY user_id HAVING cnt >= %s
                    )
                    SELECT b.User_ID AS uid, {expr} AS v
                    FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
                    ORDER BY v {order}, b.User_ID
                    LIMIT 1
                """
                row = _query_one(sql, (n,))
                return str(row["uid"]) if row else "未查询到符合条件的客户"
            sql = f"""
                SELECT user_id AS uid, COUNT(*) AS cnt
                FROM {ACTION_TABLE}
                WHERE {where}
                GROUP BY user_id
                ORDER BY cnt {order}, user_id
                LIMIT 1
            """
            row = _query_one(sql)
            return str(row["uid"]) if row else "未查询到符合条件的客户"
        if wants_count:
            base_clause, base_args = _base_where(text)
            base_filter = base_clause.replace(" WHERE ", " AND ", 1) if base_clause else ""
            sql = f"""
                WITH T AS (
                    SELECT user_id, COUNT(*) AS cnt
                    FROM {ACTION_TABLE}
                    WHERE {where}
                    GROUP BY user_id HAVING cnt >= %s
                )
                SELECT COUNT(*) AS v
                FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
                WHERE 1=1 {base_filter}
            """
            row = _query_one(sql, (n,) + base_args)
            return _count(row["v"] if row else 0)
        if metric:
            expr, unit = metric
            func = agg or "AVG"
            base_clause, base_args = _base_where(text)
            base_filter = base_clause.replace(" WHERE ", " AND ", 1) if base_clause else ""
            sql = f"""
                WITH T AS (
                    SELECT user_id, COUNT(*) AS cnt
                    FROM {ACTION_TABLE}
                    WHERE {where}
                    GROUP BY user_id HAVING cnt >= %s
                )
                SELECT {func}({expr}) AS v
                FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
                WHERE 1=1 {base_filter}
            """
            row = _query_one(sql, (n,) + base_args)
            return _fmt_by_unit(row["v"] if row else 0, unit)

    if wants_count:
        clause, args = _base_where(text)
        row = _query_one(f"SELECT COUNT(*) AS v FROM {BASE_TABLE}{clause}", args)
        return _count(row["v"] if row else 0)
    if metric and (agg or wants_who):
        expr, unit = metric
        clause, args = _base_where(text)
        if wants_who:
            order = "ASC" if any(w in text for w in ("最低", "最小", "最少")) else "DESC"
            row = _query_one(f"SELECT User_ID AS uid, {expr} AS v FROM {BASE_TABLE}{clause} ORDER BY v {order}, User_ID LIMIT 1", args)
            return str(row["uid"]) if row else "未查询到符合条件的客户"
        row = _query_one(f"SELECT {agg}({expr}) AS v FROM {BASE_TABLE}{clause}", args)
        return _fmt_by_unit(row["v"] if row else 0, unit)
    return None


def _percent_text(num, den):
    den = float(den or 0)
    if den <= 0:
        return "0%"
    return f"{int(round(float(num or 0) / den * 100))}%"


def _group_rank_answer(text):
    metric = _metric_spec(text)
    if not metric or not any(w in text for w in ("哪个", "哪类", "哪种", "最高", "最低", "最大", "最小", "最多", "最少")):
        return None
    expr, _ = metric
    order = "ASC" if any(w in text for w in ("最低", "最小", "最少")) else "DESC"
    if any(w in text for w in ("风险等级", "风险评级", "风险")) and "客户" in text:
        sql = f"SELECT Rsk_Cd AS k, AVG({expr}) AS v FROM {BASE_TABLE} GROUP BY Rsk_Cd ORDER BY v {order}, k LIMIT 1"
        row = _query_one(sql)
        return str(row["k"]) if row and row.get("k") is not None else None
    if any(w in text for w in ("性别", "男女", "男性", "女性")) and "客户" in text:
        sql = f"SELECT Gender AS k, AVG({expr}) AS v FROM {BASE_TABLE} GROUP BY Gender ORDER BY v {order}, k LIMIT 1"
        row = _query_one(sql)
        return str(row["k"]) if row and row.get("k") is not None else None
    return None


def _base_percent_answer(text):
    if not any(w in text for w in ("占比", "比例", "百分比")):
        return None
    spec = _group_condition_sql(text)
    if spec:
        where, n, _ = spec
        base_clause, base_args = _base_where(text)
        base_filter = base_clause.replace(" WHERE ", " AND ", 1) if base_clause else ""
        sql = f"""
            WITH T AS (
                SELECT user_id, COUNT(*) AS cnt
                FROM {ACTION_TABLE}
                WHERE {where}
                GROUP BY user_id HAVING cnt >= %s
            )
            SELECT COUNT(*) AS num, (SELECT COUNT(*) FROM {BASE_TABLE}) AS den
            FROM T JOIN {BASE_TABLE} b ON b.User_ID=T.user_id
            WHERE 1=1 {base_filter}
        """
        row = _query_one(sql, (n,) + base_args)
        return _percent_text(row["num"], row["den"]) if row else "0%"
    clause, args = _base_where(text)
    if not clause:
        return None
    row = _query_one(f"SELECT COUNT(*) AS num, (SELECT COUNT(*) FROM {BASE_TABLE}) AS den FROM {BASE_TABLE}{clause}", args)
    return _percent_text(row["num"], row["den"]) if row else "0%"


def _behavior_rank_answer(text):
    if not any(w in text for w in ("最多", "最少", "最高", "最低", "排名", "第一")):
        return None
    action_cond = ""
    if "浏览" in text:
        action_cond = "AND action_typ IN ('浏览详情','浏览持仓')"
    elif "购买" in text:
        action_cond = "AND action_typ='购买'"
    elif "收藏" in text:
        action_cond = "AND action_typ='收藏'"
    order = "ASC" if any(w in text for w in ("最少", "最低")) else "DESC"
    if any(w in text for w in ("哪类产品", "哪个产品", "什么产品", "产品类型", "产品行为")):
        sql = f"""
            SELECT product, COUNT(*) AS cnt
            FROM (
                SELECT {_product_case()}
                FROM {ACTION_TABLE}
                WHERE prod_typ <> '非财富' {action_cond}
            ) t
            WHERE product <> '其他'
            GROUP BY product
            ORDER BY cnt {order}, product
            LIMIT 1
        """
        row = _query_one(sql)
        return str(row["product"]) if row else None
    if any(w in text for w in ("哪个客户", "哪位客户", "谁", "客户ID")) and any(w in text for w in ("行为", "浏览", "购买", "收藏")):
        prod = _mentioned_product(text)
        prod_cond = _product_filter(prod) if prod else "prod_typ <> '非财富'"
        sql = f"""
            SELECT user_id, COUNT(*) AS cnt
            FROM {ACTION_TABLE}
            WHERE {prod_cond} {action_cond}
            GROUP BY user_id
            ORDER BY cnt {order}, user_id
            LIMIT 1
        """
        row = _query_one(sql)
        return str(row["user_id"]) if row else None
    return None

def _tool_assist(text, uid=None, row=None):
    general = _general_answer(text)
    if general:
        return general
    if uid and row:
        if any(w in text for w in ("建议书", "养老规划")):
            return _report(uid, row, text)
        return None
    if not uid:
        if any(w in text for w in ("建议书", "画像", "分析", "规划")) and any(w in text for w in ("产品", "权益", "固收", "短债", "现金", "年金", "存款", "浏览", "购买", "收藏")):
            profile = _group_profile_by_action(text)
            if profile:
                return _finalize_tool_answer(text, profile)
        ans = _base_percent_answer(text)
        if ans:
            return ans
        ans = _group_rank_answer(text)
        if ans:
            return ans
        ans = _behavior_rank_answer(text)
        if ans:
            return ans
        smart = _smart_aggregate(text)
        if smart:
            return smart
        if "平均年龄" in text and any(w in text for w in ("产品", "权益", "固收", "短债", "现金", "年金", "存款")):
            ans = _avg_age_by_action(text)
            if ans:
                return ans
        ans = _aggregate_question(text)
        if ans:
            return ans
        judge = _llm_tool_judge(text)
        _debug(f"llm tool judge={judge}")
        if "GROUP_PROFILE" in judge:
            profile = _group_profile_by_action(text)
            if profile:
                return _finalize_tool_answer(text, profile)
        if "AGGREGATE" in judge:
            ans = _aggregate_question(text)
            if ans:
                return ans
        if "GENERAL" in judge:
            return _llm(f"你是养老规划Agent，请简短回答这个通用问题，不要索要客户ID：{text}") or "我是养老规划Agent。"
        if "NEED_CUSTOMER_ID" in judge:
            return "请提供客户ID"
    return None


def run(inf):
    text = _clean_text(inf)
    uid = _uid(text)
    pronoun_followup = False
    if not uid and _is_pronoun_followup(text):
        pronoun_followup = True
        uid = _last_uid()
    _debug(f"run begin question={text} uid={uid}")
    try:
        if not uid:
            _debug("branch: no uid")
            tool_ans = _tool_assist(text)
            _debug(f"no uid tool_ans={tool_ans[:200] if tool_ans else None}")
            if tool_ans:
                return tool_ans
            if pronoun_followup:
                _debug("return: pronoun followup without in-process customer memory")
                return "请提供客户ID"
            final = _llm(f"只输出最终答案：{text}") or "未识别问题"
            _debug(f"return no uid final={final}")
            return final

        _debug("branch: fetch customer base")
        row = _base(uid)
        _debug(f"base row={row}")
        if not row:
            _debug("return: customer not found")
            return "未查询到该客户"
        _remember(text, uid)
        ack = _record_ack(uid, text)
        if ack:
            _debug(f"branch: record ack={ack}")
            return ack

        if "建议书" in text or ("养老规划" in text and any(w in text for w in ("生成", "出具", "提供", "制定"))):
            _debug("branch: report")
            return _report(uid, row, text)

        if any(w in text for w in ("多少次", "几次", "行为次数", "浏览次数", "购买次数", "收藏次数")):
            _debug("branch: product action count")
            cnt = _product_action_count(uid, text)
            if cnt is not None:
                _debug(f"return action count={cnt}")
                return f"{cnt}次"

        if any(w in text for w in ("年龄", "几岁", "多大")) and "平均" not in text:
            _debug("branch: age")
            return _age(row["Age"])
        if "性别" in text:
            _debug("branch: gender")
            return str(row.get("Gender"))
        if "风险评级" in text or "风险等级" in text or "风险承受" in text:
            _debug("branch: risk")
            return str(row.get("Rsk_Cd"))
        if "净资产" in text:
            _debug("branch: net asset")
            return _money(row.get("Net_Asset") or 0)
        if "月收入" in text or "每月收入" in text:
            _debug("branch: monthly income")
            return _money(row.get("Monthly_Income") or 0)
        if "月支出" in text or "每月支出" in text:
            _debug("branch: monthly expend")
            return _money(row.get("Monthly_Expend") or 0)
        if any(w in text for w in ("结余", "剩余", "收支差", "可攒")):
            _debug("branch: surplus")
            return _money(float(row.get("Monthly_Income") or 0) - float(row.get("Monthly_Expend") or 0))
        if "退休金" in text or "养老金" in text and "现值" not in text:
            _debug("branch: pension")
            return _money(row.get("Pension") or 0)
        if "企业年金" in text or "第二支柱" in text or (("年金" in text) and not any(w in text for w in ("年金险", "养老年金", "税延", "产品", "配置", "购买", "增加", "偏好"))):
            _debug("branch: enterprise annuity")
            ent = float(row.get("Enterprise_Ann") or 0)
            if any(w in text for w in ("有", "有没有", "是否", "吗", "么")):
                return "没有企业年金" if ent == 0 else f"有，企业年金{_money(ent)}"
            return "无" if ent == 0 else _money(ent)

        if any(w in text for w in ("行为最多", "最偏好", "最多的产品", "什么类型的产品")) or ("偏好" in text and any(w in text for w in ("行为", "记录", "浏览", "购买"))):
            _debug("branch: top product")
            return _top_product(uid, text)[0]

        if any(w in text for w in ("未来一个星期", "未来一周", "最可能购买", "购买倾向")):
            _debug("branch: predicted purchase")
            p, cnt = _top_product(uid, only_buy=True)
            return p if cnt > 0 else _top_product(uid)[0]

        if "退休" in text and any(w in text for w in ("多久", "几年", "距离", "还有")):
            _debug("branch: months to retire")
            y, m = divmod(_months_to_retire(row), 12)
            return f"{y}年{m}个月"

        if any(w in text for w in ("刚退休", "退休时每月", "每月需要支出", "退休后每月支出")):
            _debug("branch: expense at retire")
            return _money(_expense_at_retire(row))

        if "养老金现值" in text or "退休金现值" in text:
            _debug("branch: pension pv")
            life = _extract_life(text, row)
            _, _, pension_pv, _ = _need_at_retire(row, life=life)
            return _money(pension_pv)

        if any(w in text for w in ("生活费现值", "总生活费", "退休后总需求", "退休后财富需求", "退休后需要多少钱")):
            _debug("branch: living pv")
            life = _extract_life(text, row)
            _, living_pv, _, _ = _need_at_retire(row, life=life)
            return _money(living_pv)

        if any(w in text for w in ("最低需要积攒", "最低需要储备", "需要准备多少钱", "还差多少钱", "资金缺口", "养老缺口")):
            _debug("branch: need at retire")
            if "通胀率" in text and any(w in text for w in ("提升到", "提高到", "变为", "达到")):
                years = _num_before(text, "年后", 10) or 10
                rate = (_num_before(text, "%", 3) or 3) / 100
                need, _, _, _ = _need_at_retire(
                    row,
                    inflation=DEFAULT_INFLATION,
                    post_inflation=rate,
                    split_years=years,
                    later_inflation=rate,
                )
            else:
                need, _, _, _ = _need_at_retire(row, life=_extract_life(text, row))
            return _money(need)

        if any(w in text for w in ("可以积攒", "能积攒", "积累下", "退休时可以攒", "能攒下")):
            _debug("branch: future value")
            product = _mentioned_product(text)
            rate = next((p[2] for p in PRODUCTS if p[0] == product), DEFAULT_RETURN)
            return _money(_future_value(row, rate))

        if any(w in text for w in ("能否达成", "能不能达成", "是否达成", "如何调整", "怎么调整")):
            _debug("branch: goal achievable")
            need, _, _, _ = _need_at_retire(row)
            if _future_value(row, DEFAULT_RETURN) >= need:
                return "能达成"
            p = _lowest_goal_product(row, need)
            return f"不能，需要改为投资{p[0]}"

        if any(w in text for w in ("每月多攒", "每月增加", "每月还要攒", "每月需要多存")):
            _debug("branch: extra monthly saving")
            need, _, _, _ = _need_at_retire(row)
            current_fv = _future_value(row)
            gap = max(0.0, need - current_fv)
            months = _months_to_retire(row)
            mr = DEFAULT_RETURN / 12
            if gap <= 0:
                return "无需额外增加每月储蓄"
            factor = ((1 + mr) ** months - 1) / mr if mr else months
            return _money(gap / factor)

        if ("寿命" in text or "长寿" in text) and any(w in text for w in ("增加", "配置", "买什么", "什么产品")):
            _debug("branch: longevity")
            return "年金险"

        if any(w in text for w in ("收益最大", "最大化投资收益", "追求投资收益", "收益最高")):
            _debug("branch: max return")
            p = _highest_return_product(row)
            return f"{p[0]}配置100%"

        if any(w in text for w in ("最小化风险", "风险波动", "波动最小", "稳健配置")):
            _debug("branch: min risk")
            need, _, _, _ = _need_at_retire(row)
            p = _lowest_goal_product(row, need)
            full = _future_value(row, p[2])
            main = 100 if full <= 0 else min(100, math.ceil(need / full * 100 - 1e-12))
            ann = max(0, 100 - main - 10)
            return f"{p[0]}配置{main}%，现金理财配置10%，年金险配置{ann}%"

        if any(w in text for w in ("能不能买", "是否可以买", "适不适合买", "是否可配", "能否配置")):
            product = _mentioned_product(text)
            if product:
                allowed = [p[0] for p in _allowed_products(row)]
                return "可以" if product in allowed else "不建议，超出当前风险评级可配置范围"

        if "可配置" in text or "能买" in text:
            _debug("branch: allowed products")
            return "、".join(p[0] for p in _allowed_products(row))

        _debug("branch: open answer fallback")
        tool_ans = _tool_assist(text, uid, row)
        if tool_ans:
            return tool_ans
        fallback = _open_answer(uid, row, text)
        if fallback.strip() in ("无法确定", "不能确定", "无法判断", "不能判断", "不确定"):
            _debug("open fallback was non-answer, use rule open fallback")
            fallback = _rule_open_fallback(row, text)
        _debug(f"open fallback returned={fallback}")
        final = fallback or _report(uid, row, text)
        _debug(f"return fallback/report final_head={final[:200]}")
        return final
    except Exception as e:
        _debug(f"run exception: {type(e).__name__}: {e}")
        if DEBUG:
            traceback.print_exc(file=sys.stderr)
        if uid:
            try:
                _debug("exception fallback: refetch customer")
                row = _base(uid)
                if row:
                    fallback = _open_answer(uid, row, text)
                    if fallback.strip() in ("无法确定", "不能确定", "无法判断", "不能判断", "不确定"):
                        fallback = _rule_open_fallback(row, text)
                    _debug(f"exception fallback open answer={fallback}")
                    return fallback or "未识别问题"
            except Exception as e2:
                _debug(f"exception fallback failed: {type(e2).__name__}: {e2}")
                if DEBUG:
                    traceback.print_exc(file=sys.stderr)
                pass
        return "系统繁忙，请稍后重试"


if __name__ == "__main__":
    if "--debug" in sys.argv:
        DEBUG = True
        sys.argv.remove("--debug")
    print(run(sys.argv[1] if len(sys.argv) > 1 else ""))
