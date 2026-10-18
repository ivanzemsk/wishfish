"""Вишфиш — локальный прототип помощника рыбовода (Streamlit).

Приложение не управляет оборудованием и не кормит автоматически. Оно показывает
состояние по введённым данным, причины предупреждений и ПРЕДЛАГАЕТ объём
следующего кормления. Решение и фактическую выдачу корма определяет рыбовод.
"""
import csv
import io
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

APP_NAME = "Вишфиш"
DB = Path(__file__).with_name("vishfish.sqlite3")

PLACE_IN = "в закрытом помещении"
PLACE_PART = "под навесом или частично закрыт"
PLACE_OPEN = "под открытым небом"
PLACES = [PLACE_IN, PLACE_PART, PLACE_OPEN]

TZ_LABELS = {
    "Europe/Kaliningrad": "Калининград (UTC+2)", "Europe/Moscow": "Москва (UTC+3)",
    "Europe/Samara": "Самара (UTC+4)", "Asia/Yekaterinburg": "Екатеринбург (UTC+5)",
    "Asia/Omsk": "Омск (UTC+6)", "Asia/Krasnoyarsk": "Красноярск (UTC+7)",
    "Asia/Irkutsk": "Иркутск (UTC+8)", "Asia/Yakutsk": "Якутск (UTC+9)",
    "Asia/Vladivostok": "Владивосток (UTC+10)", "Asia/Magadan": "Магадан (UTC+11)",
    "Asia/Kamchatka": "Камчатка (UTC+12)", "UTC": "UTC",
}
TZ_OFFSETS = {"Europe/Kaliningrad": 2, "Europe/Moscow": 3, "Europe/Samara": 4,
              "Asia/Yekaterinburg": 5, "Asia/Omsk": 6, "Asia/Krasnoyarsk": 7,
              "Asia/Irkutsk": 8, "Asia/Yakutsk": 9, "Asia/Vladivostok": 10,
              "Asia/Magadan": 11, "Asia/Kamchatka": 12, "UTC": 0}

DEFAULTS = dict(
    obj_type="УЗВ", obj_name="Бассейн №1", tzname="Europe/Moscow", place=PLACE_IN,
    species="Форель", mass=0.0, goal="Товарное выращивание",
    o2=0.0, tw=0.0, age=0, valid="", react="обычная", left="не замечены", equip="штатно",
    wx="неизвестно", wind="неизвестно", trend="неизвестно", hpa=0.0, ta=0.0, wcom="",
    daily=0.0, feedings=0, auto_portion=False, portion=0.0, given=0.0, src="План рыбовода",
    o2min=6.0, o2crit=0.0, tmin=12.0, tmax=18.0, maxage=15, confirm=False, approver="",
    eqH=False, eqC=False, eqO=False,
    use_weather=False, coef_confirm=False,
    c_ap_norm=1.0, c_ap_low=0.75, c_lo_none=1.0, c_lo_yes=0.75,
    c_wat_ok=1.0, c_wat_bad=0.75, c_storm=0.75, c_press=0.80,
    pct_input=0.0, kg_input=0.0, share_mode="в процентах", share_touched=False,
    decision="не выбрано", basis="", comment="", reject_comment="",
    sug_action=None, demo_diff="", given_key="", flash=None, last_decision_id=None,
    feed_kg=0.0, feed_comment="",
    corr_target=None, corr_delta=0.0, corr_reason="",
    out_target=None, out_result="всё съедено", out_comment="",
)

SIG_KEYS = ["place", "o2", "tw", "age", "valid", "react", "left", "equip", "wx", "wind",
            "trend", "o2min", "o2crit", "tmin", "tmax", "maxage", "confirm",
            "eqH", "eqC", "eqO", "use_weather", "coef_confirm",
            "c_ap_norm", "c_ap_low", "c_lo_none", "c_lo_yes", "c_wat_ok", "c_wat_bad",
            "c_storm", "c_press"]

# =====================================================================
# ЛОГИКА (без Streamlit-виджетов): оценка, предложение, журнал
# =====================================================================


def nz(v):
    """0/None считаем «не введено»."""
    return v if v not in (None, 0, 0.0) else None


def fmt(n, d=2):
    if n is None:
        return "—"
    try:
        return f"{float(n):,.{d}f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def round2(n):
    return round(float(n), 2)


def floor1(x):
    return round(math.floor(x * 10 + 1e-9) / 10, 1)


def now_local(tzname):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname))
    except Exception:
        return datetime.now(timezone(timedelta(hours=TZ_OFFSETS.get(tzname, 0))))


def current_obj(s):
    return (s["obj_name"] or "").strip() or "Без названия"


def signature(s):
    return json.dumps({k: s[k] for k in SIG_KEYS}, sort_keys=True, default=str)


# ---------- оценка состояния ----------
def evaluate(s):
    items = []

    def add(g, level, text, now, lim, act):
        items.append(dict(g=g, l=level, t=text, now=now, lim=lim, act=act))

    o2, tw, age = nz(s["o2"]), nz(s["tw"]), nz(s["age"])
    o2min, tmin, tmax = float(s["o2min"]), float(s["tmin"]), float(s["tmax"])
    ocrit, maxage = nz(s["o2crit"]), nz(s["maxage"])
    hasO2, hasH, hasC = s["eqO"], s["eqH"], s["eqC"]

    # --- готовность настроек ---
    if not s["confirm"]:
        add("set", "info", "Подтвердите рабочие диапазоны в настройках",
            "галочка не поставлена", "нужна галочка подтверждения",
            "Откройте боковую панель «Рабочие диапазоны», проверьте значения и поставьте "
            "галочку. Это вопрос настроек, а не состояния рыбы.")
    problems = []
    if not o2min > 0:
        problems.append("не задан минимум кислорода")
    if not tmin < tmax:
        problems.append("минимум температуры должен быть меньше максимума")
    if ocrit is not None and ocrit >= o2min:
        problems.append("критический порог кислорода должен быть ниже рабочего минимума")
    if maxage is None:
        problems.append("не задан допустимый возраст измерений")
    ranges_ok = not problems
    if problems:
        add("set", "info", "Рабочие диапазоны заданы не полностью или некорректно",
            "; ".join(problems), "см. боковую панель", "Исправьте значения в боковой панели.")
    if ocrit is None:
        add("set", "info", "Не задан критический порог кислорода", "не задан", "нужно значение",
            "Задайте порог в боковой панели. Без него предложение по корму недоступно, "
            "а критическая тревога по кислороду не определяется.")

    # --- качество данных ---
    missing = []
    if o2 is None:
        missing.append("кислород")
    if tw is None:
        missing.append("температура воды")
    if age is None:
        missing.append("возраст измерений")
    if missing:
        add("data", "info", "Не хватает данных: " + ", ".join(missing),
            "не введено", "—", "Введите недостающие значения.")
    if s["valid"] != "да":
        add("data", "info", "Не подтверждена пригодность показаний кислорода и температуры",
            s["valid"] or "не указано", "нужно «да»",
            "Проверьте датчики или выполните ручное измерение и отметьте пригодность.")
    fresh = age is not None and maxage is not None and age <= maxage
    if age is not None and maxage is not None and age > maxage:
        add("data", "info", f"Измерениям {age} мин. В настройках допускается не более {maxage} мин.",
            f"{age} мин", f"не более {maxage} мин", "Обновите измерения.")
    data_ok = not missing and s["valid"] == "да" and fresh

    # --- состояние объекта ---
    accident = s["equip"] == "авария"
    if accident:
        add("obj", "crit", "Авария оборудования", "авария", "штатная работа",
            "Действуйте по регламенту объекта и уведомите ответственного.")

    o2_critical = False
    if data_ok and ranges_ok and s["confirm"]:
        if ocrit is not None and o2 < ocrit:
            o2_critical = True
            add("obj", "crit", f"Кислород {fmt(o2)} мг/л — ниже критического порога {fmt(ocrit)} мг/л",
                f"{fmt(o2)} мг/л", f"критический порог {fmt(ocrit)} мг/л",
                "Немедленно проверьте аэрацию и оборудование, действуйте по регламенту объекта."
                + (" Подача кислорода отмечена как доступная." if hasO2 else ""))
        elif o2 < o2min:
            add("obj", "warn", f"Кислород {fmt(o2)} мг/л — ниже рабочего минимума {fmt(o2min)} мг/л",
                f"{fmt(o2)} мг/л", f"минимум {fmt(o2min)} мг/л",
                "Проверьте аэрацию и оборудование."
                + (" Подача кислорода доступна — используйте её по регламенту объекта."
                   if hasO2 else " Подача кислорода не отмечена как доступная: "
                                 "согласуйте действия с рыбоводом."))
        if tw < tmin:
            add("obj", "warn", f"Температура воды {fmt(tw)} °C вне рабочего диапазона "
                               f"{fmt(tmin)}–{fmt(tmax)} °C (ниже)", f"{fmt(tw)} °C",
                f"{fmt(tmin)}–{fmt(tmax)} °C",
                "Проверьте подогрев; изменение температуры — постепенное, по регламенту объекта."
                if hasH else "Подогрев не отмечен как доступный: обсудите действия с рыбоводом.")
        if tw > tmax:
            add("obj", "warn", f"Температура воды {fmt(tw)} °C вне рабочего диапазона "
                               f"{fmt(tmin)}–{fmt(tmax)} °C (выше)", f"{fmt(tw)} °C",
                f"{fmt(tmin)}–{fmt(tmax)} °C",
                ("Проверьте охлаждение; изменение — постепенное, по регламенту."
                 if hasC else "Охлаждение не отмечено как доступное: согласуйте действия "
                              "с рыбоводом.") + " Контролируйте кислород.")

    refusal = s["react"] == "отказ от корма"
    if s["react"] == "сниженная":
        add("obj", "warn", "Вы отметили: сниженная реакция на корм", "сниженная", "обычная",
            "Причина не установлена. Оценку и решение по кормлению даёт рыбовод.")
    if refusal:
        add("obj", "warn", "Вы отметили: отказ от корма", "отказ от корма", "обычная",
            "Причина не установлена. Оценку и решение по кормлению даёт рыбовод.")
    if s["left"] == "замечены":
        add("obj", "warn", "Вы отметили: замечены остатки корма", "замечены", "не замечены",
            "Проверьте остатки и согласуйте объём следующей выдачи с рыбоводом.")
    if s["place"] != PLACE_IN and (s["wx"] == "гроза/шторм" or s["wind"] == "сильный"):
        kind = "открытого" if s["place"] == PLACE_OPEN else "частично укрытого"
        add("obj", "warn", f"Неблагоприятные погодные условия для {kind} объекта",
            f"погода: {s['wx']}; ветер: {s['wind']}", "гроза/шторм или сильный ветер",
            "Проверьте безопасность персонала и оборудования.")

    if any(x["l"] == "crit" for x in items):
        status, cls = "Критическая тревога", "crit"
    elif not data_ok or not ranges_ok or not s["confirm"]:
        status, cls = "Недостаточно данных", "nd"
    elif any(x["l"] == "warn" for x in items):
        status, cls = "Требуется внимание", "warn"
    else:
        status, cls = "Отклонений по доступным данным не обнаружено", "ok"

    blocked = status == "Критическая тревога" or not data_ok or not ranges_ok or not s["confirm"]
    return dict(items=items, status=status, cls=cls, blocked=blocked, data_ok=data_ok,
                ranges_ok=ranges_ok, accident=accident, refusal=refusal,
                o2_critical=o2_critical, ocrit_set=ocrit is not None)


def water_deviation(s, ev):
    if not (ev["data_ok"] and ev["ranges_ok"] and s["confirm"]):
        return []
    dev = []
    o2, tw = nz(s["o2"]), nz(s["tw"])
    if o2 is not None and o2 < float(s["o2min"]):
        dev.append(f"кислород {fmt(o2)} < {fmt(s['o2min'])} мг/л")
    if tw is not None and tw < float(s["tmin"]):
        dev.append(f"температура {fmt(tw)} °C ниже {fmt(s['tmin'])}")
    if tw is not None and tw > float(s["tmax"]):
        dev.append(f"температура {fmt(tw)} °C выше {fmt(s['tmax'])}")
    return dev


def pressure_info(s, ev):
    """Коэффициент давления: только резкое снижение, только вместе с другим признаком."""
    indoor = s["place"] == PLACE_IN
    sharp_drop = s["trend"] == "резко снижается"
    wdev = water_deviation(s, ev)
    other = s["react"] == "сниженная" or s["left"] == "замечены" or bool(wdev)
    would = (not indoor) and sharp_drop and other
    applied = would and bool(s["use_weather"])
    return dict(indoor=indoor, sharp_drop=sharp_drop, other=other, would=would,
                applied=applied, value=float(s["c_press"]) if applied else 1.0)


# ---------- предложение на следующее кормление ----------
def suggest(s, ev, given, n_done=0):
    base = float(s["portion"] or 0.0)
    daily = float(s["daily"] or 0.0)
    remainder = round2(daily - given) if daily > 0 else None
    out = dict(available=False, blockers=[], base=base, daily=daily, remainder=remainder,
               n=n_done + 1, of=int(s["feedings"] or 0), rows=[], product=None, raw=None,
               final=None, pct=None, capped_base=False, capped_remainder=False,
               pressure=pressure_info(s, ev))
    b = out["blockers"]
    if not ev["data_ok"]:
        b.append("нет актуальных данных (см. «Качество данных»)")
    if not s["confirm"] or not ev["ranges_ok"]:
        b.append("рабочие диапазоны не подтверждены или заданы некорректно")
    if ev["accident"]:
        b.append("авария оборудования")
    if ev["refusal"]:
        b.append("отмечен отказ от корма")
    if not ev["ocrit_set"]:
        b.append("не задан критический порог кислорода")
    if ev["o2_critical"]:
        b.append("кислород ниже критического порога")
    if not s["coef_confirm"]:
        b.append("коэффициенты не подтверждены в настройках")
    if base <= 0:
        b.append("плановая разовая порция равна нулю или не задана")
    if remainder is not None and remainder <= 0:
        b.append("остаток суточного плана равен нулю (план исполнен или превышен на "
                 f"{fmt(abs(remainder))} кг)" if remainder < 0 else
                 "остаток суточного плана равен нулю")
    if b:
        return out

    rows = []
    rows.append(dict(name="Аппетит", value=float(s["c_ap_low"] if s["react"] == "сниженная"
                                                  else s["c_ap_norm"]),
                     note="сниженная реакция" if s["react"] == "сниженная" else "обычная реакция"))
    left = s["left"] == "замечены"
    rows.append(dict(name="Остатки корма", value=float(s["c_lo_yes"] if left else s["c_lo_none"]),
                     note="остатки замечены" if left else "остатки не замечены"))
    wdev = water_deviation(s, ev)
    rows.append(dict(name="Состояние воды", value=float(s["c_wat_bad"] if wdev else s["c_wat_ok"]),
                     note=("вне диапазона: " + "; ".join(wdev)) if wdev
                     else "кислород и температура в рабочем диапазоне"))
    p = out["pressure"]
    wval, parts = 1.0, []
    if s["place"] == PLACE_IN:
        parts.append("закрытое помещение: всегда 1,0")
    elif not s["use_weather"]:
        parts.append("погодная корректировка выключена")
    else:
        if s["wx"] == "гроза/шторм" or s["wind"] == "сильный":
            wval *= float(s["c_storm"])
            parts.append(f"гроза/сильный ветер ×{fmt(s['c_storm'])}")
        if p["applied"]:
            wval *= float(s["c_press"])
            parts.append(f"резкое снижение давления ×{fmt(s['c_press'])} (есть другой признак)")
        elif p["sharp_drop"] and not p["other"]:
            parts.append("резкое снижение давления без других признаков — не применено")
        if not parts:
            parts.append("неблагоприятных условий нет")
    rows.append(dict(name="Погода", value=wval, note="; ".join(parts)))

    prod = 1.0
    for r in rows:
        prod *= r["value"]
    raw = base * prod
    x = min(raw, base)
    out["capped_base"] = raw > base + 1e-9
    final = round(x + 1e-9, 1)
    if final > base:
        final = floor1(base)
    if remainder is not None and final > remainder:
        final = floor1(remainder)
        out["capped_remainder"] = True
    out.update(rows=rows, product=prod, raw=raw, final=final)
    if final <= 0:
        b.append("расчётный объём меньше 0,1 кг (проверьте коэффициенты и остаток)")
        return out
    out["pct"] = round2(final / base * 100)
    out["available"] = True
    return out


def operator_action(sug, action, chosen_kg):
    if action == "отклонено":
        return "отклонено"
    if not sug.get("available"):
        return "без предложения"
    if chosen_kg is None:
        return "не рассмотрено"
    same = abs(chosen_kg - sug["final"]) < 0.005
    if action == "принято":
        return "принято" if same else "изменено"
    if action == "изменено":
        return "изменено"
    return "введено вручную"


def decision_payload(s, ev, sug, chosen_kg, chosen_pct, eval_time, given):
    action = operator_action(sug, s["sug_action"], chosen_kg)
    p = sug["pressure"]
    applied = bool(sug.get("available") and p["applied"])
    coefs = "; ".join(f"{r['name']}={fmt(r['value'])}" for r in sug["rows"]) if sug["rows"] else ""
    wdev = water_deviation(s, ev)
    return dict(
        evaluated_utc=eval_time, status=ev["status"],
        reasons=" | ".join(x["t"] for x in ev["items"]),
        place=s["place"], type=s["obj_type"], species=s["species"], mass_g=s["mass"], goal=s["goal"],
        o2=s["o2"], water_t=s["tw"], age_min=s["age"], valid=s["valid"],
        reaction=s["react"], leftovers=s["left"], equipment=s["equip"],
        weather=s["wx"], wind=s["wind"], pressure_trend=s["trend"], pressure_hpa=s["hpa"],
        air_t=s["ta"], weather_comment=s["wcom"], norm_source=s["src"],
        daily_plan_kg=s["daily"], feedings_per_day=s["feedings"], planned_portion_kg=s["portion"],
        given_before_kg=given, remainder_kg=sug["remainder"],
        ranges_confirmed=s["confirm"], approver=s["approver"],
        coef_confirmed=s["coef_confirm"], weather_correction_enabled=s["use_weather"],
        suggestion_available=sug["available"], suggestion_blockers=" | ".join(sug["blockers"]),
        base_kg=sug["base"], coefficients=coefs, coef_product=sug["product"],
        suggested_kg=sug["final"], suggested_pct=sug["pct"],
        capped_by_remainder=sug["capped_remainder"],
        pressure_coef_applied=applied, pressure_coef_value=p["value"] if applied else 1.0,
        pressure_coef_would_apply=p["would"],
        reduced_reaction=s["react"] == "сниженная", leftovers_seen=s["left"] == "замечены",
        water_deviation="; ".join(wdev),
        accepted_kg=chosen_kg, accepted_pct=chosen_pct, operator_action=action,
        decision=s["decision"], basis=s["basis"], comment=s["comment"],
    )


# ---------- журнал (SQLite, записи только добавляются) ----------
COLS = ["id", "created_utc", "local_date", "local_time", "object_name", "kind", "kg",
        "ref_id", "comment", "payload"]


@contextmanager
def db():
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL, "
            "local_date TEXT NOT NULL, local_time TEXT NOT NULL, object_name TEXT NOT NULL, "
            "kind TEXT NOT NULL, kg REAL, ref_id INTEGER, comment TEXT, payload TEXT)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_record(kind, obj, tzname, kg=None, ref_id=None, comment="", payload=None):
    now = now_local(tzname)
    with db() as c:
        cur = c.execute(
            "INSERT INTO records(created_utc,local_date,local_time,object_name,kind,kg,ref_id,"
            "comment,payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (now.astimezone(timezone.utc).isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
             now.strftime("%H:%M:%S"), obj, kind, kg, ref_id, comment or "",
             json.dumps(payload or {}, ensure_ascii=False, default=str)))
        return cur.lastrowid


def day_records(obj, date):
    with db() as c:
        rows = c.execute("SELECT " + ",".join(COLS) + " FROM records WHERE object_name=? AND "
                         "local_date=? ORDER BY id", (obj, date)).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def given_sum(obj, date):
    with db() as c:
        v = c.execute("SELECT COALESCE(SUM(kg),0) FROM records WHERE object_name=? AND "
                      "local_date=? AND kind IN ('feeding','correction')", (obj, date)).fetchone()[0]
    return round2(v)


def feeding_count(obj, date):
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM records WHERE object_name=? AND local_date=? "
                         "AND kind='feeding'", (obj, date)).fetchone()[0]


def all_records():
    with db() as c:
        rows = c.execute("SELECT " + ",".join(COLS) + " FROM records ORDER BY id").fetchall()
    return [dict(zip(COLS, r)) for r in rows]


KIND_LABEL = {"feeding": "Выдача", "correction": "Корректировка", "outcome": "Итог кормления",
              "decision": "Решение"}


def day_table(recs):
    """Таблица за сутки: исходные выдачи, корректировки и нарастающий итог."""
    corr = {}
    for r in recs:
        if r["kind"] == "correction" and r["ref_id"]:
            corr[r["ref_id"]] = corr.get(r["ref_id"], 0.0) + (r["kg"] or 0.0)
    rows, total = [], 0.0
    for r in recs:
        k = r["kind"]
        if k == "decision":
            continue
        if k in ("feeding", "correction"):
            total += r["kg"] or 0.0
        try:
            payload = json.loads(r["payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        row = {"№": r["id"], "Время": r["local_time"], "Запись": KIND_LABEL.get(k, k),
               "Рекомендация, кг": fmt(payload.get("suggested_kg")) if payload.get("suggested_kg") is not None else "—",
               "Фактически, кг": fmt(payload.get("actual_kg", r["kg"])) if k == "feeding" else "",
               "К записи №": r["ref_id"] or "",
               "Объём, кг": fmt(r["kg"]) if r["kg"] is not None and k != "feeding" else "",
               "Итог по выдаче, кг": fmt((r["kg"] or 0.0) + corr.get(r["id"], 0.0))
               if k == "feeding" else "",
               "Итого за сутки, кг": fmt(total) if k in ("feeding", "correction") else "",
               "Комментарий / причина": r["comment"] or ""}
        if k == "outcome":
            try:
                row["Комментарий / причина"] = (json.loads(r["payload"]).get("result", "")
                                                + (": " + r["comment"] if r["comment"] else ""))
            except ValueError:
                pass
        rows.append(row)
    return rows, round2(total)


def decisions_table(recs):
    rows = []
    for r in recs:
        if r["kind"] != "decision":
            continue
        try:
            p = json.loads(r["payload"])
        except ValueError:
            p = {}
        rows.append({"№": r["id"], "Время": r["local_time"], "Статус": p.get("status", ""),
                     "Предложено, кг": fmt(p.get("suggested_kg")) if p.get("suggested_kg") is not None else "—",
                     "Принято, кг": fmt(p.get("accepted_kg")) if p.get("accepted_kg") is not None else "—",
                     "Действие": p.get("operator_action", ""), "Решение": p.get("decision", ""),
                     "Основание": p.get("basis", "") or r["comment"] or ""})
    return rows


def records_to_csv(recs):
    base = ["id", "created_utc", "local_date", "local_time", "object_name", "kind", "kg",
            "ref_id", "comment"]
    parsed, extra = [], []
    for r in recs:
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            p = {}
        parsed.append(p)
        for k in p:
            if k not in extra and k not in base:
                extra.append(k)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(base + extra)
    for r, p in zip(recs, parsed):
        row = [r[k] for k in base]
        for k in extra:
            v = p.get(k, "")
            row.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        w.writerow(["" if v is None else v for v in row])
    return out.getvalue()


# =====================================================================
# ИНТЕРФЕЙС
# =====================================================================
st.set_page_config(page_title=APP_NAME, layout="wide")
for _k, _v in DEFAULTS.items():
    st.session_state.setdefault(_k, _v)


def flash(scope, kind, text):
    st.session_state.flash = dict(scope=scope, kind=kind, text=text)


def show_flash(scope):
    f = st.session_state.get("flash")
    if f and f["scope"] == scope:
        getattr(st, f["kind"])(f["text"])
        st.session_state.flash = None


def yn(v):
    if v is True:
        return "да"
    if v is False:
        return "нет"
    return "пусто" if v in (None, "") else str(v)


# ---------- демо ----------
DEMOS = {
    "Норма": dict(o2=8.0, tw=15.0, age=5, valid="да", react="обычная", left="не замечены", equip="штатно"),
    "Отклонение": dict(o2=5.5, tw=19.0, age=5, valid="да", react="сниженная", left="не замечены", equip="штатно"),
    "Нет данных": dict(o2=8.0, tw=15.0, age=30, valid="нет", react="обычная", left="не замечены", equip="штатно"),
    "Авария": dict(o2=4.5, tw=15.0, age=5, valid="да", react="обычная", left="не замечены", equip="авария"),
}
DEMO_LABELS = {"o2": "Кислород", "tw": "Температура воды", "age": "Возраст измерений",
               "valid": "Пригодность", "react": "Реакция", "left": "Остатки",
               "equip": "Оборудование", "confirm": "Диапазоны подтверждены",
               "coef_confirm": "Коэффициенты подтверждены", "daily": "Суточный план",
               "feedings": "Число кормлений", "auto_portion": "Порция автоматически",
               "o2crit": "Критический порог O₂"}


def apply_values(vals):
    s, ch = st.session_state, []
    for k, v in vals.items():
        if s[k] != v:
            ch.append(f"{DEMO_LABELS[k]}: {yn(s[k])} → {yn(v)}")
        s[k] = v
    s.demo_diff = "Изменено: " + ("; ".join(ch) if ch else "ничего, значения уже совпадали")


def do_evaluate():
    s = st.session_state
    ev = evaluate(s)
    s.result = dict(sig=signature(s), ev=ev,
                    time=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    s.share_touched = False
    s.pct_input = 0.0
    s.kg_input = 0.0
    s.share_mode = "в процентах"
    s.sug_action = None


def cb_demo(name):
    vals = dict(DEMOS[name])
    vals["confirm"] = True
    vals["coef_confirm"] = True
    apply_values(vals)
    do_evaluate()


def cb_demo_feed():
    apply_values(dict(daily=500.0, feedings=5, auto_portion=True, o2crit=4.0))


# ---------- журнал выдач ----------
def cb_recalc_given():
    s = st.session_state
    now = now_local(s.tzname)
    s.given = float(given_sum(current_obj(s), now.strftime("%Y-%m-%d")))


def cb_record_feeding():
    s = st.session_state
    kg = float(s.feed_kg or 0.0)
    if kg <= 0:
        flash("feed", "warning", "Укажите объём выдачи больше нуля.")
        return
    obj, now = current_obj(s), now_local(s.tzname)
    date = now.strftime("%Y-%m-%d")
    before = given_sum(obj, date)
    daily = float(s.daily or 0.0)
    payload = dict(daily_plan_kg=daily, planned_portion_kg=s.portion, given_before_kg=before,
                   suggested_kg=s.get("current_suggested_kg"), actual_kg=round2(kg),
                   recommendation_status=("подтверждена" if s.get("current_suggested_kg") is not None
                                          and abs(kg - float(s.current_suggested_kg)) < 1e-9
                                          else "скорректирована" if s.get("current_suggested_kg") is not None
                                          else "без рекомендации"),
                   exceeds_daily_plan=bool(daily > 0 and before + kg > daily + 1e-9))
    add_record("feeding", obj, s.tzname, kg=round2(kg), comment=s.feed_comment, payload=payload)
    suggested = s.get("current_suggested_kg")
    if suggested is not None:
        if abs(kg - float(suggested)) < 1e-9:
            message = f"Рекомендация подтверждена: рекомендовано {fmt(suggested)} кг, выдано {fmt(kg)} кг."
        else:
            message = f"Выдача записана: рекомендовано {fmt(suggested)} кг, фактически выдано {fmt(kg)} кг."
    else:
        message = f"Фактическая выдача записана: {fmt(kg)} кг."
    s.feed_kg = 0.0
    s.feed_comment = ""
    flash("feed", "success", message + " Сумма за сутки пересчитана.")


def cb_correction():
    s = st.session_state
    obj, date = current_obj(s), s.get("view_date_str")
    target, delta, reason = s.corr_target, float(s.corr_delta or 0.0), (s.corr_reason or "").strip()
    if target is None or not date:
        flash("journal", "warning", "Нет записи выдачи для корректировки.")
        return
    if abs(delta) < 1e-9:
        flash("journal", "warning", "Укажите поправку в кг (не ноль).")
        return
    if not reason:
        flash("journal", "warning", "Причина корректировки обязательна.")
        return
    recs = day_records(obj, date)
    cur = sum((r["kg"] or 0.0) for r in recs
              if (r["kind"] == "feeding" and r["id"] == target) or
              (r["kind"] == "correction" and r["ref_id"] == target))
    if cur + delta < -1e-9:
        flash("journal", "warning", f"Итог по выдаче не может стать отрицательным (сейчас {fmt(cur)} кг).")
        return
    add_record("correction", obj, s.tzname, kg=round2(delta), ref_id=target, comment=reason,
               payload=dict(reason=reason, original_total_before=cur))
    s.corr_delta = 0.0
    s.corr_reason = ""
    flash("journal", "success", "Корректирующая запись добавлена. Исходная запись не изменена.")


def cb_outcome():
    s = st.session_state
    if s.out_target is None:
        flash("journal", "warning", "Нет записи выдачи для отметки итога.")
        return
    add_record("outcome", current_obj(s), s.tzname, ref_id=s.out_target, comment=s.out_comment,
               payload=dict(result=s.out_result))
    s.out_comment = ""
    flash("journal", "success", "Итог кормления записан.")


# ---------- предложение и решение ----------
def cb_accept(kg, pct):
    s = st.session_state
    s.sug_action = "принято"
    s.share_mode = "в процентах"
    s.pct_input = float(pct)
    s.kg_input = float(kg)
    s.share_touched = True
    flash("sug", "success", "Предложение принято: объём подставлен в поля ниже. "
                            "Это ещё не запись о выдаче.")


def cb_change(kg, pct):
    s = st.session_state
    s.sug_action = "изменено"
    s.share_mode = "в кг"
    s.pct_input = float(pct)
    s.kg_input = float(kg)
    s.share_touched = True
    flash("sug", "info", "Измените объём в полях ниже и при желании укажите основание.")


def cb_reject(sug):
    s = st.session_state
    res = s.get("result")
    if not res or res["sig"] != signature(s):
        return
    s.sug_action = "отклонено"
    payload = decision_payload(s, res["ev"], sug, None, None, res["time"], float(s.given or 0.0))
    payload["reject_comment"] = s.reject_comment
    rid = add_record("decision", current_obj(s), s.tzname, comment=s.reject_comment, payload=payload)
    s.last_decision_id = rid
    s.reject_comment = ""
    flash("sug", "success", "Отклонение предложения записано в журнал.")


def cb_chip(p):
    s = st.session_state
    P = float(s.portion or 0.0)
    s.share_mode = "в процентах"
    s.pct_input = float(p)
    s.kg_input = round2(P * p / 100) if P > 0 else 0.0
    s.share_touched = True


def cb_pct():
    s = st.session_state
    P = float(s.portion or 0.0)
    s.share_mode = "в процентах"
    s.kg_input = round2(P * s.pct_input / 100)
    s.share_touched = True


def cb_kg():
    s = st.session_state
    P = float(s.portion or 0.0)
    s.share_mode = "в кг"
    s.pct_input = round2(s.kg_input / P * 100) if P > 0 else 0.0
    s.share_touched = True


def cb_kg_only():
    st.session_state.share_touched = True


def cb_save_decision(sug):
    s = st.session_state
    res = s.get("result")
    if not res or res["sig"] != signature(s):
        flash("dec", "warning", "Данные изменились после оценки. Оцените заново.")
        return
    ev = res["ev"]
    chosen_kg = chosen_pct = None
    if not ev["blocked"] and s.share_touched:
        chosen_kg = round2(s.kg_input)
        chosen_pct = round2(s.pct_input) if float(s.portion or 0.0) > 0 else None
    payload = decision_payload(s, ev, sug, chosen_kg, chosen_pct, res["time"], float(s.given or 0.0))
    rid = add_record("decision", current_obj(s), s.tzname, comment=s.comment, payload=payload)
    s.last_decision_id = rid
    flash("dec", "success", "Решение сохранено в журнале. Это запись решения, а не подтверждение "
                            "выдачи корма: выдачу вносите отдельно ниже.")


# ====================== САЙДБАР ======================
with st.sidebar:
    st.header("Профиль и настройки")
    st.subheader("Объект")
    st.selectbox("Тип объекта", ["УЗВ", "Пруд", "Садки"], key="obj_type")
    st.text_input("Название объекта", key="obj_name")
    st.selectbox("Размещение", PLACES, key="place")
    if st.session_state.place == PLACE_IN:
        st.caption("Объект в помещении: осадки и наружный ветер не используются в оценке "
                   "состояния бассейна.")
        weather_enabled = False
        st.session_state.wx = "неизвестно"
        st.session_state.wind = "неизвестно"
    else:
        weather_enabled = True
        if st.session_state.place == PLACE_PART:
            st.caption("Навес не считается равнозначным помещению.")
    st.text_input("Вид рыбы", key="species")
    st.number_input("Средняя масса рыбы, г", min_value=0.0, step=10.0, key="mass")
    st.selectbox("Цель выращивания",
                 ["Товарное выращивание", "Маточное стадо / икра", "Молодь / воспроизводство"],
                 key="goal")
    st.selectbox("Часовой пояс", list(TZ_LABELS), key="tzname", format_func=lambda k: TZ_LABELS[k])

    st.divider()
    st.subheader("Рабочие диапазоны")
    st.number_input("Минимум кислорода, мг/л", min_value=0.0, step=0.1, key="o2min")
    st.number_input("Критический порог кислорода, мг/л", min_value=0.0, step=0.1, key="o2crit")
    st.caption("0 = не задан. Порог нужен для критической тревоги и для предложения по корму.")
    st.number_input("Минимум температуры воды, °C", step=0.5, key="tmin")
    st.number_input("Максимум температуры воды, °C", step=0.5, key="tmax")
    st.number_input("Допустимый возраст измерений, мин", min_value=0, step=1, key="maxage")
    st.checkbox("Рабочие диапазоны проверены и подтверждены", key="confirm")
    st.text_input("Кто согласовал / источник (необязательно)", key="approver")
    st.caption("Для продолжения достаточно галочки; поле не обязательно.")

    st.divider()
    st.subheader("Оборудование: доступные средства")
    st.checkbox("Есть подогрев", key="eqH")
    st.checkbox("Есть охлаждение", key="eqC")
    st.checkbox("Есть подача кислорода", key="eqO")

    st.divider()
    with st.expander("Коэффициенты предложения"):
        st.caption("Стартовые значения демонстрационные, это не проверенные нормы. "
                   "Предложение показывается только после подтверждения коэффициентов. "
                   "Отказ от корма = 0 и блокировка (не настраивается).")
        st.checkbox("Учитывать погодную корректировку", key="use_weather")
        st.caption("Выключено: коэффициент погоды всегда 1,0. Включено: гроза/сильный ветер "
                   "на незакрытом объекте и резкое снижение давления (только вместе с "
                   "другим признаком). Закрытое помещение — всегда 1,0.")
        ca, cb = st.columns(2)
        for col, (label, key) in zip([ca, cb] * 4, [
                ("Реакция обычная", "c_ap_norm"), ("Реакция сниженная", "c_ap_low"),
                ("Остатки не замечены", "c_lo_none"), ("Остатки замечены", "c_lo_yes"),
                ("Вода в диапазоне", "c_wat_ok"), ("Вода вне диапазона", "c_wat_bad"),
                ("Гроза / сильный ветер", "c_storm"), ("Резкое снижение давления", "c_press")]):
            col.number_input(label, min_value=0.0, max_value=1.5, step=0.05, key=key)
        st.checkbox("Коэффициенты проверены и подтверждены", key="coef_confirm")

# ====================== ШАПКА ======================
st.title(APP_NAME)
st.caption("Прототип. Рекомендация — подсказка; фактическую выдачу подтверждает рыбовод.")

with st.expander("Демонстрационные сценарии"):
    dcols = st.columns(4)
    for col, name in zip(dcols, DEMOS):
        col.button(name, key=f"demo_{name}", on_click=cb_demo, args=(name,))
    st.button("Подставить пример данных кормления и порога кислорода", on_click=cb_demo_feed)
    st.caption("Тестовые сценарии, а не рабочие режимы. Сценарий заполняет измерения, ставит "
               "галочки подтверждения диапазонов и коэффициентов и сразу выполняет оценку; "
               "границы диапазонов не меняются. Кнопка «пример данных» задаёт суточный план "
               "500 кг, 5 кормлений, автоматический расчёт порции и демонстрационный порог "
               "кислорода 4 мг/л — это не норма.")
    if st.session_state.demo_diff:
        st.info(st.session_state.demo_diff)

with st.expander("О роли рыбовода и ограничениях"):
    st.caption("Нет подключения к датчикам, внешней погоде и оборудованию; все данные "
               "вводятся вручную. Нет автоматического кормления и расчёта времени до гибели. "
               "Статус «отклонений не обнаружено» не гарантирует безопасность. Предложение по "
               "корму — подсказка по введённым данным; решение и фактическую выдачу корма "
               "определяет рыбовод. Данные о связи давления и поедаемости пилот только "
               "накапливает: по малому числу ручных записей причинную связь доказать нельзя.")

# ---------- дата, объект, сумма по журналу ----------
_now = now_local(st.session_state.tzname)
today = _now.strftime("%Y-%m-%d")
obj = current_obj(st.session_state)
jsum = given_sum(obj, today)
_key = f"{obj}|{today}|{jsum}"
if st.session_state.given_key != _key:
    st.session_state.given = float(jsum)
    st.session_state.given_key = _key

# ====================== ОСНОВНЫЕ БЛОКИ ======================
col_meas, col_wx, col_feed = st.columns(3)

with col_meas:
    st.subheader("Измерения")
    st.number_input("Кислород, мг/л", min_value=0.0, step=0.1, key="o2")
    st.number_input("Температура воды, °C", step=0.5, key="tw")
    st.number_input("Возраст измерений, мин", min_value=0, step=1, key="age")
    st.selectbox("Пригодность показаний", ["", "да", "нет"], key="valid")
    st.selectbox("Реакция на корм", ["обычная", "сниженная", "отказ от корма"], key="react")
    st.selectbox("Остатки корма", ["не замечены", "замечены"], key="left")
    st.selectbox("Оборудование", ["штатно", "авария"], key="equip")

with col_wx:
    st.subheader("Погода и давление")
    st.selectbox("Погода", ["неизвестно", "ясно", "облачно", "дождь", "снег", "гроза/шторм"],
                 key="wx", disabled=not weather_enabled)
    st.selectbox("Ветер", ["неизвестно", "штиль", "слабый", "умеренный", "сильный"],
                 key="wind", disabled=not weather_enabled)
    st.selectbox("Изменение давления",
                 ["неизвестно", "стабильно", "растёт", "снижается", "резко растёт", "резко снижается"],
                 key="trend")
    st.number_input("Давление, гПа (необязательно)", step=1.0, key="hpa")
    st.number_input("Температура воздуха, °C", step=0.5, key="ta")
    st.text_input("Комментарий (необязательно)", key="wcom")

with col_feed:
    st.subheader("Кормление")
    st.number_input("Суточный план, кг", min_value=0.0, step=0.5, key="daily")
    st.number_input("Число кормлений в сутки", min_value=0, step=1, key="feedings")
    st.checkbox("Рассчитать плановую порцию: суточный план ÷ число кормлений", key="auto_portion")
    if st.session_state.auto_portion:
        _n = int(st.session_state.feedings or 0)
        _d = float(st.session_state.daily or 0.0)
        st.session_state.portion = round2(_d / _n) if _n > 0 and _d > 0 else 0.0
    st.number_input("Плановая разовая порция, кг", min_value=0.0, step=0.1, key="portion",
                    disabled=st.session_state.auto_portion)
    st.number_input("Уже выдано за сутки, кг", min_value=0.0, step=0.1, key="given")
    st.caption(f"Сумма записей журнала за {today} по объекту «{obj}»: {fmt(jsum)} кг")
    if abs(float(st.session_state.given) - jsum) > 0.005:
        st.warning("Значение отличается от суммы записей журнала")
    st.button("Пересчитать по журналу", on_click=cb_recalc_given)
    st.selectbox("Источник кормовой нормы",
                 ["План рыбовода", "Таблица производителя", "Демонстрационный пример"], key="src")
    _daily = float(st.session_state.daily or 0.0)
    if _daily > 0:
        st.caption(f"Остаток суточного плана (справочно): "
                   f"{fmt(_daily - float(st.session_state.given))} кг")

st.divider()
st.subheader("Результат")
st.button("Оценить", type="primary", on_click=do_evaluate)

STATUS_RENDER = {"crit": st.error, "nd": st.info, "warn": st.warning, "ok": st.success}
GROUP_NAMES = {"obj": "Состояние объекта", "data": "Качество данных", "set": "Готовность настроек"}

result = st.session_state.get("result")
if not result:
    st.caption("Заполните данные и нажмите «Оценить».")
else:
    ev = result["ev"]
    stale = result["sig"] != signature(st.session_state)
    if stale:
        st.warning("Данные изменились после оценки. Нажмите «Оценить» повторно.")
    STATUS_RENDER[ev["cls"]](ev["status"])
    if ev["cls"] == "ok":
        st.caption("Это не гарантия безопасности: отклонения не найдены только по введённым данным.")
    for g in ["obj", "data", "set"]:
        gi = [x for x in ev["items"] if x["g"] == g]
        st.markdown(f"**{GROUP_NAMES[g]}**")
        if not gi:
            st.caption("Замечаний нет.")
        for x in gi:
            st.markdown(f"- **{x['t']}**  \n  Сейчас: {x['now']} · Граница: {x['lim']}  \n"
                        f"  Действие: {x['act']}")
    ctx = []
    if st.session_state.place != PLACE_IN:
        if st.session_state.wx != "неизвестно":
            ctx.append("погода: " + st.session_state.wx)
        if st.session_state.wind != "неизвестно":
            ctx.append("ветер: " + st.session_state.wind)
    if st.session_state.trend != "неизвестно":
        ctx.append("давление: " + st.session_state.trend)
    if nz(st.session_state.hpa):
        ctx.append(fmt(st.session_state.hpa, 1) + " гПа")
    if nz(st.session_state.ta):
        ctx.append("воздух " + fmt(st.session_state.ta, 1) + " °C")
    if ctx:
        st.caption("Контекст: " + " · ".join(ctx))

    given_now = float(st.session_state.given or 0.0)
    sug = suggest(st.session_state, ev, given_now, feeding_count(obj, today))

    # ---------- Предложение на следующее кормление ----------
    st.subheader("Предложение на следующее кормление")
    show_flash("sug")
    st.session_state.current_suggested_kg = None
    if stale:
        st.warning("Предложение недоступно: оцените заново.")
    elif not sug["available"]:
        st.warning("Предложение не показывается: " + "; ".join(sug["blockers"]) + ".")
        st.caption("Корм не назначается автоматически, в том числе «половина».")
    else:
        head = f"Кормление {sug['n']} из {sug['of']}" if sug["of"] > 0 else f"Кормление №{sug['n']}"
        if sug["of"] > 0 and sug["n"] > sug["of"]:
            head += " (сверх плана по числу кормлений)"
        st.markdown(f"**{head}**. База — плановая разовая порция: **{fmt(sug['base'])} кг**")
        st.table([{"Коэффициент": r["name"], "Значение": fmt(r["value"]), "Основание": r["note"]}
                  for r in sug["rows"]])
        st.markdown(f"{fmt(sug['base'])} кг × {fmt(sug['product'], 4)} = {fmt(sug['raw'])} кг")
        if sug["capped_base"]:
            st.caption("Предложение не превышает плановую разовую порцию.")
        if sug["capped_remainder"]:
            st.warning(f"Предложение уменьшено до остатка суточного плана: "
                       f"{fmt(sug['remainder'])} кг (суточный план {fmt(sug['daily'])} кг, "
                       f"уже выдано {fmt(given_now)} кг).")
        st.success(f"Рекомендация на следующее кормление: **{fmt(sug['final'])} кг**")
        st.caption("Проверьте рекомендацию. Ниже можно оставить это количество или изменить его; "
                   "в журнал попадут рекомендация и фактическая выдача.")
        st.session_state.current_suggested_kg = round2(sug["final"])
        _suggestion_key = f"{result['time']}|{round2(sug['final'])}"
        if st.session_state.get("feed_suggestion_key") != _suggestion_key:
            st.session_state.feed_kg = round2(sug["final"])
            st.session_state.feed_suggestion_key = _suggestion_key

# ====================== ФАКТИЧЕСКАЯ ВЫДАЧА ======================
st.divider()
st.subheader("Подтверждение выдачи корма")
show_flash("feed")
st.caption("По умолчанию указана рекомендация системы. Оставьте её, чтобы подтвердить рекомендацию, "
           "или измените количество на фактически выданный объём. В журнале сохранятся оба значения. "
           "При отсутствии рекомендации можно внести фактическую выдачу вручную.")
st.number_input("Фактически выдано, кг", min_value=0.0, step=0.1, key="feed_kg")
st.text_input("Комментарий к выдаче", key="feed_comment")
_fk, _fd = float(st.session_state.feed_kg or 0.0), float(st.session_state.daily or 0.0)
_gv = float(st.session_state.given or 0.0)
if _fd > 0 and _fk > 0 and _gv + _fk > _fd + 1e-9:
    st.warning(f"Превышение суточного плана: после записи выдано будет {fmt(_gv + _fk)} кг "
               f"при плане {fmt(_fd)} кг. Запись всё равно возможна: она фиксирует факт.")
elif _fd > 0 and _fd - _gv <= 0:
    st.warning("Суточный план уже исполнен. Запись выдачи будет превышением плана.")
st.button("Подтвердить и записать выдачу", type="primary", on_click=cb_record_feeding, disabled=_fk <= 0)

# ====================== ЖУРНАЛ ======================
st.divider()
st.subheader("Журнал")
show_flash("journal")
view_date = st.date_input("Дата журнала", value=_now.date())
view_str = view_date.strftime("%Y-%m-%d")
st.session_state.view_date_str = view_str
recs = day_records(obj, view_str)
rows, day_total = day_table(recs)
st.metric(f"Выдано за {view_str} по объекту «{obj}», кг", fmt(day_total))
if rows:
    st.dataframe(rows, hide_index=True)
else:
    st.caption("Записей выдачи за эту дату нет.")

feeds = [r for r in recs if r["kind"] == "feeding"]
if feeds:
    ids = [r["id"] for r in feeds]
    labels = {r["id"]: f"№{r['id']} · {r['local_time']} · {fmt(r['kg'])} кг" for r in feeds}
    for _k in ("corr_target", "out_target"):
        if st.session_state.get(_k) not in ids:
            st.session_state[_k] = ids[-1]
    with st.expander("Корректирующая запись (исходная запись не меняется)"):
        st.selectbox("Запись выдачи", ids, key="corr_target", format_func=lambda i: labels[i])
        st.number_input("Поправка, кг (+/−)", min_value=-1000.0, max_value=1000.0, step=0.1,
                        key="corr_delta")
        st.text_input("Причина (обязательно)", key="corr_reason")
        st.button("Добавить корректирующую запись", on_click=cb_correction)
    with st.expander("Итог кормления"):
        st.selectbox("Запись выдачи", ids, key="out_target", format_func=lambda i: labels[i])
        st.selectbox("Что произошло после кормления",
                     ["всё съедено", "остались остатки", "отказ / почти не ел"], key="out_result")
        st.text_input("Комментарий к итогу", key="out_comment")
        st.button("Записать итог кормления", on_click=cb_outcome)

_all = all_records()
if _all:
    st.download_button("Выгрузить CSV (все записи)", records_to_csv(_all).encode("utf-8-sig"),
                       "vishfish_log.csv", "text/csv")

st.caption("Локальный прототип без авторизации: не размещайте производственные данные публично. "
           "Журнал хранится рядом с приложением в vishfish.sqlite3 (записи только добавляются).")
