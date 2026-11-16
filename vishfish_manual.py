"""Вишфиш — прототип помощника рыбовода (Streamlit).

Приложение ничем не управляет и никого не кормит. Оно смотрит на введённые вручную
данные, говорит, что в бассейне не так, и подсказывает, сколько дать на следующее
кормление. Решение и кормление остаются за рыбоводом.
"""
import csv
import io
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st

DB = Path(__file__).with_name("vishfish.sqlite3")
UNKNOWN_UNIT = "Единица не установлена"

DEFAULTS = dict(
    obj_name="Бассейн 1",
    o2=None, tw=None, age=0, react="как обычно", left="нет", equip="работает",
    daily=0.0, feedings=0,
    o2min=7.0, o2crit=0.0, tmin=12.0, tmax=16.0, maxage=15,
    c_ap=0.75, c_lo=0.75, c_wat=0.75,
    feed_kg=0.0, comment="", corr_target=None, corr_delta=0.0, corr_reason="", flash=None,
    ph_min=7.0, ph_max=8.0, co2_max=10.0, chem_maxage=60,
    nh3_max=0.025, nitrite_max=0.2, nitrate_max=50.0,
    check_nitrite=False, check_nitrate=False,
    demo_mode=False,
)

# Единица — часть измерения: мг/л и мг N/л нельзя сравнивать напрямую.
PARAMS = {
    "tw": ("Температура воды", ["°C"]),
    "o2": ("Растворённый кислород", ["мг/л O₂"]),
    "ph": ("pH", ["pH"]),
    "co2": ("Растворённый CO₂", ["мг/л CO₂"]),
    "ammonia": ("Аммиак / аммоний", [UNKNOWN_UNIT, "TAN, мг N/л", "NH₄⁺, мг/л", "NH₄-N, мг N/л", "NH₃, мг/л"]),
    "nitrite": ("Нитрит", [UNKNOWN_UNIT, "NO₂⁻, мг/л", "NO₂-N, мг N/л"]),
    "nitrate": ("Нитрат", [UNKNOWN_UNIT, "NO₃⁻, мг/л", "NO₃-N, мг N/л"]),
}

SOURCES = {
    "trout": "https://science.rspca.org.uk/documents/d/science/1856_farmed_trout_welfare_standards_web",
    "ammonia": "https://ask.ifas.ufl.edu/publication/FA031",
    "equilibrium": "https://www.epa.gov/sites/default/files/2019-02/documents/update-wqc-ammonia-1998.pdf",
}

# =====================================================================
# ЛОГИКА (без виджетов Streamlit)
# =====================================================================


def fmt(n, d=1):
    if n is None:
        return "—"
    return f"{float(n):,.{d}f}".replace(",", " ").replace(".", ",")


def browser_timezone():
    """Часовой пояс браузера, а не сервера Streamlit."""
    name = st.context.timezone
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    offset = st.context.timezone_offset
    if offset is not None:
        # Браузерный getTimezoneOffset: для Москвы это -180 минут.
        return timezone(-timedelta(minutes=offset))
    return datetime.now().astimezone().tzinfo


def local_now():
    return datetime.now(timezone.utc).astimezone(browser_timezone())


def local_record_time(record):
    return datetime.fromisoformat(record["created_utc"]).astimezone(browser_timezone())


def floor1(x):
    return round(math.floor(x * 10 + 1e-9) / 10, 1)


def fresh_measurement(chem, key, minutes):
    measurement = chem.get(key)
    return measurement if measurement and measurement["age_minutes"] <= minutes else None


def free_ammonia_mg_l(ammonia, ph, temperature):
    """NH₃ в мг/л; расчёт из TAN-N для пресной воды по Emerson et al."""
    if ammonia["unit"] == "NH₃, мг/л":
        return ammonia["value"]
    if ammonia["unit"] != "TAN, мг N/л" or ph is None or temperature is None:
        return None
    pka = 0.09018 + 2729.92 / (273.15 + temperature)
    fraction = 1 / (1 + 10 ** (pka - ph))
    return ammonia["value"] * fraction * 17.031 / 14.007


def nitrogen_ion_mg_l(measurement, ion_unit, nitrogen_unit, factor):
    """Приводит результат теста к мг/л иона; неизвестную единицу не угадывает."""
    if measurement["unit"] == ion_unit:
        return measurement["value"]
    if measurement["unit"] == nitrogen_unit:
        return measurement["value"] * factor
    return None


def evaluate(s):
    """Что сейчас не так. Возвращает статус и список (уровень, что, что делать)."""
    items, water = [], []

    def add(level, text, todo):
        items.append((level, text, todo))

    o2, tw = s["o2"], s["tw"]
    o2min, o2crit, tmin, tmax = s["o2min"], s["o2crit"], s["tmin"], s["tmax"]

    problems = []
    if tmin >= tmax:
        problems.append("нормы температуры заданы неверно")
        add("nd", "Нормы температуры заданы неверно: «от» должно быть меньше «до»",
            "Поправьте нормы в боковой панели.")
    if s["ph_min"] and s["ph_max"] and s["ph_min"] >= s["ph_max"]:
        problems.append("границы pH заданы неверно")
        add("nd", "Нижняя граница pH должна быть меньше верхней",
            "Поправьте значения в боковой панели.")
    missing = [n for n, v in (("кислород", o2), ("температура воды", tw)) if v is None]
    if missing:
        problems.append("не введено: " + ", ".join(missing))
        add("nd", "Не введено: " + ", ".join(missing), "Введите результаты замера.")
    fresh = s["age"] <= s["maxage"]
    if not fresh:
        problems.append("замер устарел")
        add("nd", f"Замеру {s['age']} мин, актуальным считается до {s['maxage']} мин",
            "Сделайте новый замер и введите его.")
    data_ok = not problems

    accident = s["equip"] == "авария"
    if accident:
        add("crit", "Авария оборудования",
            "Действуйте по инструкции хозяйства.")

    o2_crit = False
    if data_ok:
        if o2crit and o2 < o2crit:
            o2_crit = True
            add("crit", f"Кислород {fmt(o2)} мг/л — критически низкий (ниже {fmt(o2crit)})",
                "Срочно проверьте аэрацию и подачу кислорода, действуйте по инструкции.")
        elif o2 < o2min:
            water.append(f"кислород {fmt(o2)} мг/л ниже нормы")
            add("warn", f"Кислород {fmt(o2)} мг/л — ниже нормы ({fmt(o2min)})",
                "Проверьте аэрацию и подачу кислорода.")
        if tw < tmin:
            water.append(f"вода холоднее нормы ({fmt(tw)} °C)")
            add("warn", f"Температура воды {fmt(tw)} °C — ниже нормы ({fmt(tmin)}–{fmt(tmax)})",
                "Проверьте подогрев. Температуру нужно менять плавно.")
        if tw > tmax:
            water.append(f"вода теплее нормы ({fmt(tw)} °C)")
            add("warn", f"Температура воды {fmt(tw)} °C — выше нормы ({fmt(tmin)}–{fmt(tmax)})",
                "Проверьте охлаждение и кислород. Температуру нужно менять постепенно.")

    if s["react"] == "вяло":
        add("warn", "Рыба ест корм слабо",
            "Выясните причину: кислород, температура, состояние рыбы. Сколько кормить: решает рыбовод.")
    if s["react"] == "отказывается":
        add("warn", "Рыба отказывается от корма",
            "Выясните причину: кислород, температура, состояние рыбы. Сколько кормить: решает рыбовод.")
    if s["left"] == "есть":
        add("warn", "После кормления остался корм",
            "Осмотрите бассейн. Уменьшать ли следующую порцию решает рыбовод.")
    chem = s.get("chem", {})
    ph = fresh_measurement(chem, "ph", s["chem_maxage"])
    if ph:
        value = ph["value"]
        if s["ph_min"] and value < s["ph_min"]:
            add("warn", f"pH {fmt(value, 2)} — ниже установленного диапазона",
                "Проверьте прибор и сообщите технологу.")
        if s["ph_max"] and value > s["ph_max"]:
            add("warn", f"pH {fmt(value, 2)} — выше установленного диапазона",
                "Проверьте прибор и сообщите технологу.")
    co2 = fresh_measurement(chem, "co2", s["chem_maxage"])
    if co2 and s["co2_max"] and co2["value"] > s["co2_max"]:
        add("warn", f"Растворённый CO₂ {fmt(co2['value'])} мг/л — выше установленного предела",
            "Проверьте замер и обратитесь к технологу по регламенту хозяйства.")

    ammonia = fresh_measurement(chem, "ammonia", s["chem_maxage"])
    temperature = fresh_measurement(chem, "tw", s["maxage"])
    nh3 = (free_ammonia_mg_l(ammonia, ph["value"] if ph else None,
                            temperature["value"] if temperature else None)
           if ammonia else None)
    if nh3 is not None and s["nh3_max"] and nh3 >= s["nh3_max"]:
        add("warn", f"Свободный NH₃ {fmt(nh3, 4)} мг/л — достиг установленного предела",
            "Проверьте замеры, работу биофильтра и свяжитесь с технологом.")

    nitrite = fresh_measurement(chem, "nitrite", s["chem_maxage"])
    if nitrite and s["check_nitrite"] and s["nitrite_max"]:
        value = nitrogen_ion_mg_l(nitrite, "NO₂⁻, мг/л", "NO₂-N, мг N/л", 46.0055 / 14.0067)
        if value is not None and value >= s["nitrite_max"]:
            add("warn", f"Нитрит NO₂⁻ {fmt(value, 3)} мг/л — достиг установленного предела",
                "Повторите измерение и сообщите технологу; проверьте биофильтр.")
    nitrate = fresh_measurement(chem, "nitrate", s["chem_maxage"])
    if nitrate and s["check_nitrate"] and s["nitrate_max"]:
        value = nitrogen_ion_mg_l(nitrate, "NO₃⁻, мг/л", "NO₃-N, мг N/л", 62.0049 / 14.0067)
        if value is not None and value >= s["nitrate_max"]:
            add("warn", f"Нитрат NO₃⁻ {fmt(value, 2)} мг/л — достиг установленного предела",
                "Повторите измерение и обсудите водообмен с технологом.")

    levels = [lv for lv, _, _ in items]
    status = next((lv for lv in ("crit", "nd", "warn") if lv in levels), "ok")
    return dict(items=items, status=status, data_ok=data_ok, problems=problems, accident=accident,
                o2_crit=o2_crit, refusal=s["react"] == "отказывается", water=water,
                nh3=nh3)


def suggest(s, ev, given):
    """Сколько дать на следующее кормление: обычная порция × поправки."""
    daily, n = float(s["daily"] or 0), int(s["feedings"] or 0)
    base = round(daily / n, 2) if daily > 0 and n > 0 else 0.0
    left = round(daily - given, 2) if daily > 0 else None
    out = dict(ok=False, why=[], base=base, left=left, rows=[], final=None, pct=None,
               cut=False)
    why = out["why"]
    if not ev["data_ok"]:
        why.append("нет данных для расчёта (" + "; ".join(ev["problems"]) + ")")
    if ev["accident"]:
        why.append("авария оборудования")
    if ev["o2_crit"]:
        why.append("кислород критически низкий")
    if ev["refusal"]:
        why.append("рыба отказывается от корма")
    if base <= 0:
        why.append("не указано, сколько корма на сутки и сколько раз будут кормить")
    if left is not None and left <= 0:
        why.append("корм на сегодня уже скормлен по норме")

    if why:
        return out

    rows = [
        ("Как рыба ест корм", s["c_ap"] if s["react"] == "вяло" else 1.0,
         "вяло" if s["react"] == "вяло" else "как обычно"),
        ("Остаток корма", s["c_lo"] if s["left"] == "есть" else 1.0,
         "корм остался" if s["left"] == "есть" else "корма не осталось"),
        ("Вода", s["c_wat"] if ev["water"] else 1.0,
         "; ".join(ev["water"]) or "кислород и температура в норме"),
    ]
    product = 1.0
    for _, v, _ in rows:
        product *= v
    final = round(min(base * product, base) + 1e-9, 1)
    if final > base:
        final = floor1(base)
    if left is not None and final > left:
        final, out["cut"] = floor1(left), True
    out.update(rows=rows, final=final)
    if final <= 0:
        why.append("получилось меньше 0,1 кг")
        return out
    out.update(ok=True, pct=round(final / base * 100))
    return out


# ---------- журнал (SQLite, записи только добавляются) ----------
COLS = ["id", "created_utc", "local_date", "local_time", "object_name", "kind", "kg",
        "ref_id", "comment", "payload"]
KINDS = {"feeding": "Скормлено", "correction": "Исправление", "rejected": "Отказ от предложения"}


@contextmanager
def db():
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT NOT NULL, "
            "local_date TEXT NOT NULL, local_time TEXT NOT NULL, object_name TEXT NOT NULL, "
            "kind TEXT NOT NULL, kg REAL, ref_id INTEGER, comment TEXT, payload TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS measurements ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, object_name TEXT NOT NULL, "
            "parameter TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, "
            "measured_utc TEXT NOT NULL, recorded_utc TEXT NOT NULL, "
            "source TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '')")
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_record(kind, obj, kg=None, ref_id=None, comment="", payload=None):
    now = local_now()
    with db() as c:
        c.execute("INSERT INTO records(created_utc,local_date,local_time,object_name,kind,kg,"
                  "ref_id,comment,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), obj, kind, kg, ref_id,
                   comment or "", json.dumps(payload or {}, ensure_ascii=False, default=str)))


def read_records(obj=None):
    """Записи за сегодня по бассейну; без аргумента — вообще все записи."""
    sql, args = "SELECT " + ",".join(COLS) + " FROM records", ()
    if obj is not None:
        today = local_now().date()
        tz = browser_timezone()
        start = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        sql += " WHERE object_name=? AND created_utc>=? AND created_utc<?"
        args = (obj, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
    with db() as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def payload_of(r):
    try:
        return json.loads(r["payload"] or "{}")
    except ValueError:
        return {}


def add_measurements(obj, measured_at, values, comment=""):
    """Сохраняем каждый показатель отдельно, даже если замеряли их одновременно."""
    measured_utc = measured_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    recorded_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        c.executemany(
            "INSERT INTO measurements(object_name,parameter,value,unit,measured_utc,"
            "recorded_utc,source,comment) VALUES (?,?,?,?,?,?,?,?)",
            [(obj, key, float(value), unit, measured_utc, recorded_utc, "вручную", comment)
             for key, (value, unit) in values.items()])


def read_measurements(obj):
    with db() as c:
        rows = c.execute(
            "SELECT id,object_name,parameter,value,unit,measured_utc,recorded_utc,source,comment "
            "FROM measurements WHERE object_name=? ORDER BY measured_utc DESC,id DESC",
            (obj,)).fetchall()
    keys = ("id", "object_name", "parameter", "value", "unit", "measured_utc",
            "recorded_utc", "source", "comment")
    return [dict(zip(keys, row)) for row in rows]


def latest_measurements(recs):
    latest = {}
    now = datetime.now(timezone.utc)
    for r in recs:
        if r["parameter"] not in latest:
            age = (now - datetime.fromisoformat(r["measured_utc"])).total_seconds() / 60
            latest[r["parameter"]] = {**r, "age_minutes": max(0, age)}
    return latest


def journal_rows(recs):
    """Таблица за сегодня и сумма скормленного (выдачи + исправления)."""
    rows, total = [], 0.0
    for r in recs:
        if r["kind"] not in KINDS:
            continue
        counted = r["kind"] in ("feeding", "correction")
        if counted:
            total += r["kg"] or 0.0
        sug = payload_of(r).get("suggested_kg")
        name = KINDS[r["kind"]] + (f" к №{r['ref_id']}" if r["kind"] == "correction" else "")
        rows.append({"№": r["id"], "Время": local_record_time(r).strftime("%H:%M:%S"), "Запись": name,
                     "кг": fmt(r["kg"]) if r["kg"] is not None else "",
                     "Предложено, кг": fmt(sug) if sug is not None else "",
                     "Всего за сутки, кг": fmt(total) if counted else "",
                     "Комментарий": r["comment"] or ""})
    return rows, round(total, 2)


def csv_text(recs):
    payloads = [payload_of(r) for r in recs]
    extra = sorted({k for p in payloads for k in p})
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["№", "Дата", "Время", "Бассейн", "Запись", "кг", "К записи №", "Комментарий"] + extra)
    for r, p in zip(recs, payloads):
        local = local_record_time(r)
        row = [r["id"], local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S"), r["object_name"],
               KINDS.get(r["kind"], r["kind"]), r["kg"], r["ref_id"], r["comment"]]
        w.writerow(["" if v is None else v for v in row + [p.get(k, "") for k in extra]])
    return out.getvalue()


# =====================================================================
# ИНТЕРФЕЙС
# =====================================================================
st.set_page_config(page_title="Вишфиш", layout="wide")
for _k, _v in DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
S = st.session_state
if S.get("_trout_profile_version") != 1:
    # При обновлении открытой вкладки заменяем только прежние значения по умолчанию.
    for _key, _old, _new in (("o2min", 6.0, 7.0), ("o2crit", 4.0, 0.0),
                             ("tmax", 18.0, 16.0), ("ph_min", 0.0, 7.0),
                             ("ph_max", 0.0, 8.0), ("co2_max", 0.0, 10.0)):
        if S.get(_key) == _old:
            S[_key] = _new
    S._trout_profile_version = 1

DEMOS = {
    "Норма": dict(o2=8.0, tw=15.0, age=0, react="как обычно", left="нет", equip="работает"),
    "Отклонение": dict(o2=5.5, tw=19.0, age=0, react="вяло", left="нет", equip="работает"),
    "Нет данных": dict(o2=8.0, tw=15.0, age=30, react="как обычно", left="нет", equip="работает"),
    "Авария": dict(o2=4.5, tw=15.0, age=0, react="как обычно", left="нет", equip="авария"),
}


def obj_name():
    return (S.obj_name or "").strip() or "Без названия"


def flash(scope, text, kind="success"):
    S.flash = dict(scope=scope, text=text, kind=kind)


def show_flash(scope):
    f = S.get("flash")
    if f and f["scope"] == scope:
        getattr(st, f["kind"])(f["text"])
        S.flash = None


def current_state():
    """Оценка, предложение и сумма за сегодня по текущим значениям."""
    recs = read_records(obj_name())
    rows, given = journal_rows(recs)
    latest = latest_measurements(read_measurements(obj_name()))
    state = dict(S)
    if not S.demo_mode:
        state["o2"] = latest.get("o2", {}).get("value")
        state["tw"] = latest.get("tw", {}).get("value")
        state["age"] = max((latest[k]["age_minutes"] for k in ("o2", "tw") if k in latest), default=0)
    state["chem"] = latest
    ev = evaluate(state)
    return recs, rows, given, ev, suggest(state, ev, given)


def cb_demo(name):
    for k, v in DEMOS[name].items():
        S[k] = v
    S.demo_mode = True
    if not S.daily:
        S.daily = 500.0
    if not S.feedings:
        S.feedings = 5
    flash("demo", f"Подставлен пример «{name}».", "info")


def exit_demo():
    S.demo_mode = False
    S.react, S.left, S.equip = "как обычно", "нет", "работает"


def cb_accept(kg):
    S.feed_kg = float(kg)
    flash("sug", "Объём подставлен в блок «Записать кормление». Пока это не запись.", "info")


def cb_feed():
    if S.demo_mode:
        flash("feed", "Выключите демо-режим перед записью реального кормления.", "warning")
        return
    kg = float(S.feed_kg or 0)
    if kg <= 0:
        flash("feed", "Введите, сколько корма скормили (больше нуля).", "warning")
        return
    _, _, given, ev, sg = current_state()
    if sg["ok"]:
        action = "по предложению" if abs(kg - sg["final"]) < 0.005 else "не как в предложении"
    else:
        action = "без предложения"
    daily = float(S.daily or 0)
    latest = latest_measurements(read_measurements(obj_name()))
    add_record("feeding", obj_name(), kg=round(kg, 2), comment=S.comment, payload=dict(
        suggested_kg=sg["final"] if sg["ok"] else None, action=action,
        adjustments="; ".join(f"{n}={fmt(v, 2)}" for n, v, _ in sg["rows"]),
        status=ev["status"], o2=latest.get("o2", {}).get("value"),
        water_t=latest.get("tw", {}).get("value"), reaction=S.react, leftovers=S.left,
        daily_norm_kg=daily, one_feeding_kg=sg["base"], given_before_kg=given,
        over_daily_norm=bool(daily > 0 and given + kg > daily + 1e-9)))
    S.feed_kg = 0.0
    S.comment = ""
    flash("feed", f"Записано: скормлено {fmt(kg)} кг.")


def cb_reject():
    if S.demo_mode:
        flash("feed", "Выключите демо-режим перед записью отказа.", "warning")
        return
    _, _, _, ev, sg = current_state()
    if not sg["ok"]:
        return
    add_record("rejected", obj_name(), comment=S.comment, payload=dict(
        suggested_kg=sg["final"], status=ev["status"],
        adjustments="; ".join(f"{n}={fmt(v, 2)}" for n, v, _ in sg["rows"]),
        reaction=S.react, leftovers=S.left))
    S.comment = ""
    flash("feed", "Отказ от предложения записан.")


def cb_correct():
    delta, reason = float(S.corr_delta or 0), (S.corr_reason or "").strip()
    if S.corr_target is None or abs(delta) < 1e-9:
        flash("journal", "Выберите запись и введите поправку в кг (не ноль).", "warning")
        return
    if not reason:
        flash("journal", "Укажите причину исправления.", "warning")
        return
    now_kg = sum((r["kg"] or 0.0) for r in read_records(obj_name())
                 if r["id"] == S.corr_target or r["ref_id"] == S.corr_target)
    if now_kg + delta < -1e-9:
        flash("journal", f"Так получится меньше нуля (сейчас по этой записи {fmt(now_kg)} кг).", "warning")
        return
    add_record("correction", obj_name(), kg=round(delta, 2), ref_id=S.corr_target, comment=reason)
    S.corr_delta, S.corr_reason = 0.0, ""
    flash("journal", "Исправление добавлено. Исходная запись не менялась.")


# ---------- боковая панель ----------
with st.sidebar:
    st.header("Бассейн")
    st.text_input("Название", key="obj_name")
    st.caption("Радужная форель · УЗВ")

    st.header("Нормы")
    st.caption("Профиль для форели в УЗВ. Значения можно менять.")
    st.number_input("Кислород: норма от, мг/л", min_value=0.0, step=0.1, key="o2min")
    st.number_input("Кислород: аварийный уровень, мг/л (0 = не задан)", min_value=0.0, step=0.1, key="o2crit")
    st.number_input("Температура воды от, °C", step=0.5, key="tmin")
    st.number_input("Температура воды до, °C", step=0.5, key="tmax")
    st.number_input("Замер свежий, не старше, мин", min_value=0, step=1, key="maxage")
    with st.expander("Пределы pH, CO₂ и соединений азота"):
        st.number_input("pH от (0 = не задано)", min_value=0.0, max_value=14.0,
                        step=0.1, key="ph_min")
        st.number_input("pH до (0 = не задано)", min_value=0.0, max_value=14.0,
                        step=0.1, key="ph_max")
        st.number_input("CO₂ максимум, мг/л (0 = не задано)", min_value=0.0,
                        step=0.1, key="co2_max")
        st.number_input("Свободный NH₃ максимум, мг/л (0 = не задано)", min_value=0.0,
                        step=0.001, format="%.3f", key="nh3_max")
        st.caption("Для TAN-N свободный NH₃ рассчитывается только при свежих pH и температуре. "
                   "NH₄⁺ с TAN не приравнивается.")
        st.number_input("Нитрит NO₂⁻ максимум, мг/л", min_value=0.0,
                        step=0.01, key="nitrite_max")
        st.checkbox("Учитывать нитрит", key="check_nitrite")
        st.number_input("Нитрат NO₃⁻ максимум, мг/л", min_value=0.0,
                        step=1.0, key="nitrate_max")
        st.checkbox("Учитывать нитрат", key="check_nitrate")
        st.number_input("Актуальность pH, CO₂ и азота, мин", min_value=1,
                        step=1, key="chem_maxage")
        st.link_button("Источник порогов: RSPCA", SOURCES["trout"])
        st.link_button("Расчёт NH₃: UF/IFAS", SOURCES["ammonia"])

    with st.expander("Поправки к порции"):
        for _label, _key in [("Рыба берёт корм слабо", "c_ap"), ("Остался корм", "c_lo"),
                             ("Вода вне нормы", "c_wat")]:
            st.number_input(_label, min_value=0.0, max_value=1.0, step=0.05, key=_key)
        st.caption("1,0 — порция не меняется. Значения стартовые, на практике не проверены.")

# ---------- шапка ----------
st.title("Вишфиш")
st.caption("Прототип. Сохраняет ручные замеры воды и кормления. "
           "Решение по кормлению принимает рыбовод.")
with st.expander("Примеры для показа"):
    for _col, _name in zip(st.columns(4), DEMOS):
        _col.button(_name, key=f"demo_{_name}", on_click=cb_demo, args=(_name,))
    st.caption("Подставляют замеры. Если не указан корм на сутки, ставят 500 кг и 5 кормлений.")
    show_flash("demo")
if S.demo_mode:
    st.warning("Демо-режим: показания кислорода и температуры сейчас примерные.")
    if st.button("Вернуться к реальным замерам"):
        exit_demo()
        st.rerun()

# ---------- ввод замеров воды ----------
st.subheader("Записать замеры воды")
st.caption("Вносите только измеренные показатели. Если время замеров различается, сохраните их "
           "отдельными записями. CO₂ здесь означает растворённый CO₂ в воде.")
with st.form("water_measurements", clear_on_submit=True):
    date_col, time_col = st.columns(2)
    measured_date = date_col.date_input("Дата замера", value=local_now().date())
    measured_time = time_col.time_input("Время замера", value=local_now().time().replace(second=0, microsecond=0))
    raw, units = {}, {}
    for key, (label, available_units) in PARAMS.items():
        value_col, unit_col = st.columns([3, 2])
        raw[key] = value_col.text_input(label, placeholder="Оставьте пустым, если не измеряли", key=f"input_{key}")
        if len(available_units) == 1:
            units[key] = available_units[0]
            unit_col.text_input("Единица измерения", value=units[key], disabled=True,
                                key=f"fixed_unit_{key}")
        else:
            units[key] = unit_col.selectbox("Единица измерения", available_units,
                                            key=f"unit_{key}")
    measure_comment = st.text_input("Примечание к этим замерам")
    submitted = st.form_submit_button("Сохранить замеры")
if submitted:
    values, errors = {}, []
    for key, entered in raw.items():
        if not entered.strip():
            continue
        try:
            value = float(entered.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            errors.append(f"{PARAMS[key][0]}: введите число")
            continue
        if not math.isfinite(value) or value < 0 or (key == "ph" and not 0 < value <= 14):
            errors.append(f"{PARAMS[key][0]}: проверьте значение")
            continue
        values[key] = (value, units[key])
    when = datetime.combine(measured_date, measured_time, tzinfo=browser_timezone())
    if when.astimezone(timezone.utc) > datetime.now(timezone.utc):
        errors.append("Время измерения не может быть в будущем")
    if not values:
        errors.append("Введите хотя бы один показатель")
    if errors:
        st.error("; ".join(errors))
    else:
        add_measurements(obj_name(), when, values, measure_comment)
        if S.demo_mode:
            exit_demo()
        st.success(f"Сохранено измерений: {len(values)}")

# ---------- состояние бассейна и кормление ----------
c1, c2 = st.columns(2)
with c1:
    st.subheader("Наблюдения")
    st.caption("Кислород и температура берутся из сохранённых замеров воды.")
    st.selectbox("Как рыба берёт корм", ["как обычно", "вяло", "отказывается"], key="react")
    st.selectbox("Остался ли корм после кормления", ["нет", "есть"], key="left")
    st.selectbox("Оборудование", ["работает", "авария"], key="equip")
with c2:
    st.subheader("Корм на сегодня")
    st.caption("Укажите суточную норму и число кормлений.")
    st.number_input("Всего корма на сутки, кг", min_value=0.0, step=0.5, key="daily")
    st.number_input("Кормлений в сутки", min_value=0, step=1, key="feedings")

recs, jrows, given, ev, sg = current_state()
m1, m2, m3 = st.columns(3)
m1.metric("Одно кормление, кг", fmt(sg["base"]) if sg["base"] else "—")
m2.metric("Уже скормлено сегодня, кг", fmt(given))
m3.metric("Осталось на сегодня, кг", fmt(sg["left"]) if sg["left"] is not None else "—")

# ---------- что происходит ----------
st.divider()
st.subheader("Что происходит")
STATUS = {"crit": ("Тревога", st.error), "nd": ("Не хватает данных", st.info),
          "warn": ("Нужно обратить внимание", st.warning),
          "ok": ("По проверяемым показателям отклонений нет", st.success)}
STATUS[ev["status"]][1](STATUS[ev["status"]][0])
if ev["status"] == "ok":
    st.caption("Учитываются только показатели, для которых заданы правила и есть актуальные замеры. "
               "Отсутствие предупреждения не означает, что все параметры воды безопасны.")
for _lv, _text, _todo in ev["items"]:
    st.markdown(f"- **{_text}**  \n  Что делать: {_todo}")
if ev["nh3"] is not None:
    st.caption(f"Расчётный свободный NH₃: {fmt(ev['nh3'], 4)} мг/л. "
               "Он не меняет порцию автоматически.")

# ---------- предложение ----------
st.subheader("Сколько дать на следующее кормление")
show_flash("sug")
if not sg["ok"]:
    st.warning("Предложения отсутствуют: " + "; ".join(sg["why"]) + ".")
    st.caption("Если решите кормить, запишите кормление ниже вручную.")
else:
    _done = sum(1 for r in recs if r["kind"] == "feeding")
    _n = int(S.feedings or 0)
    st.markdown(f"**Кормление {_done + 1} из {_n}.** Обычная порция: {fmt(sg['base'])} кг")
    st.table([{"Что учли": n, "Поправка": fmt(v, 2), "Почему": why} for n, v, why in sg["rows"]])
    if sg["cut"]:
        st.warning(f"Уменьшено до того, что осталось на сегодня: {fmt(sg['left'])} кг.")
    st.success(f"Предложение: {fmt(sg['final'])} кг ({sg['pct']}% обычной порции)")
    st.caption("Это подсказка на одно следующее кормление, а не суточная норма. "
               "Поправки стартовые, на практике не проверены.")
    st.button("Принять предложение", on_click=cb_accept, args=(sg["final"],))

# ---------- запись кормления ----------
st.divider()
st.subheader("Записать кормление")
show_flash("feed")
st.number_input("Скормлено, кг", min_value=0.0, step=0.1, key="feed_kg")
st.text_input("Комментарий (необязательно)", key="comment")
_kg = float(S.feed_kg or 0)
_daily = float(S.daily or 0)
if _daily > 0 and _kg > 0 and given + _kg > _daily + 1e-9:
    st.warning(f"Больше суточной нормы: с этой записью будет {fmt(given + _kg)} кг из {fmt(_daily)} кг. "
               "Записать всё равно можно.")
elif _daily > 0 and _daily - given <= 0:
    st.caption("На сегодня корм по норме уже скормлен.")
b1, b2 = st.columns(2)
b1.button("Записать кормление", type="primary", on_click=cb_feed, disabled=_kg <= 0)
b2.button("Отказаться от предложения", on_click=cb_reject, disabled=not sg["ok"])
st.caption("Записывайте то, что реально скормили. Предложение и отказ тоже попадают в журнал.")

# ---------- журнал ----------
st.divider()
st.subheader(f"Журнал за сегодня: {obj_name()}")
show_flash("journal")
st.metric("Скормлено за сегодня, кг", fmt(given))
if jrows:
    st.dataframe(jrows, hide_index=True)
else:
    st.caption("Записей за сегодня нет.")

st.divider()
st.subheader("История качества воды")
measurements = read_measurements(obj_name())
latest = latest_measurements(measurements)
if latest:
    current_rows = []
    for key, (label, _) in PARAMS.items():
        if key not in latest:
            continue
        m = latest[key]
        current_rows.append({"Показатель": label, "Значение": fmt(m["value"], 2),
                             "Единица": m["unit"], "Время замера":
                             datetime.fromisoformat(m["measured_utc"]).astimezone(browser_timezone()).strftime("%d.%m.%Y %H:%M"),
                             "Возраст, мин": round(m["age_minutes"]), "Источник": m["source"]})
    st.dataframe(current_rows, hide_index=True)
    st.caption("В расчётах и предупреждениях используются актуальные замеры с известной единицей. "
               "Нитрит и нитрат учитываются, если включены в настройках.")
    selected = st.selectbox("График показателя", list(PARAMS),
                            format_func=lambda key: PARAMS[key][0])
    selected_units = latest[selected]["unit"] if selected in latest else PARAMS[selected][1][0]
    points = [m for m in measurements if m["parameter"] == selected and m["unit"] == selected_units]
    if points:
        frame = pd.DataFrame([{"Время": datetime.fromisoformat(m["measured_utc"]).astimezone(browser_timezone()),
                               "Значение": m["value"]} for m in reversed(points)])
        st.line_chart(frame.set_index("Время"), y="Значение")
        st.caption(f"На графике только измерения в единице «{selected_units}».")
    st.dataframe([{"Время": datetime.fromisoformat(m["measured_utc"]).astimezone(browser_timezone()).strftime("%d.%m.%Y %H:%M"),
                   "Показатель": PARAMS[m["parameter"]][0], "Значение": fmt(m["value"], 2),
                   "Единица": m["unit"], "Источник": m["source"], "Примечание": m["comment"]}
                  for m in measurements[:100]], hide_index=True)
    water_csv = io.StringIO()
    writer = csv.writer(water_csv, delimiter=";")
    writer.writerow(["бассейн", "показатель", "значение", "единица", "время UTC", "источник", "примечание"])
    for m in reversed(measurements):
        writer.writerow([m["object_name"], PARAMS[m["parameter"]][0], m["value"], m["unit"],
                         m["measured_utc"], m["source"], m["comment"]])
    st.download_button("Скачать замеры воды (CSV)", water_csv.getvalue().encode("utf-8-sig"),
                       "vishfish_water.csv", "text/csv")
else:
    st.caption("Замеров пока нет. Сохраните первый замер выше.")

_feeds = [r for r in recs if r["kind"] == "feeding"]
if _feeds:
    _ids = [r["id"] for r in _feeds]
    _names = {r["id"]: f"№{r['id']} · {local_record_time(r).strftime('%H:%M:%S')} · {fmt(r['kg'])} кг"
              for r in _feeds}
    if S.get("corr_target") not in _ids:
        S.corr_target = _ids[-1]
    with st.expander("Исправить запись"):
        st.caption("Старая запись не меняется: добавляется исправление с причиной.")
        st.selectbox("Какая запись", _ids, key="corr_target", format_func=lambda i: _names[i])
        st.number_input("Поправка, кг (плюс или минус)", min_value=-1000.0, max_value=1000.0,
                        step=0.1, key="corr_delta")
        st.text_input("Причина (обязательно)", key="corr_reason")
        st.button("Добавить исправление", on_click=cb_correct)

_all = read_records()
if _all:
    st.download_button("Скачать весь журнал (CSV)", csv_text(_all).encode("utf-8-sig"),
                       "vishfish_journal.csv", "text/csv")
st.caption("Журнал хранится в файле vishfish.sqlite3 рядом с программой. "
           "Не выкладывайте реальные данные хозяйства в открытый доступ.")
