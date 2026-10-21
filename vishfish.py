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
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

DB = Path(__file__).with_name("vishfish.sqlite3")
INDOOR, SHADE, OPEN = "в помещении", "под навесом", "на открытом воздухе"

DEFAULTS = dict(
    obj_name="Бассейн 1", obj_type="УЗВ", species="Форель", place=INDOOR,
    o2=0.0, tw=0.0, age=0, react="как обычно", left="нет", equip="работает",
    wx="ясно", wind="слабый", trend="стабильное",
    daily=0.0, feedings=0,
    o2min=6.0, o2crit=4.0, tmin=12.0, tmax=18.0, maxage=15,
    use_weather=False, c_ap=0.75, c_lo=0.75, c_wat=0.75, c_storm=0.75, c_press=0.80,
    feed_kg=0.0, comment="", corr_target=None, corr_delta=0.0, corr_reason="", flash=None,
)

# =====================================================================
# ЛОГИКА (без виджетов Streamlit)
# =====================================================================


def fmt(n, d=1):
    if n is None:
        return "—"
    return f"{float(n):,.{d}f}".replace(",", " ").replace(".", ",")


def floor1(x):
    return round(math.floor(x * 10 + 1e-9) / 10, 1)


def evaluate(s):
    """Что сейчас не так. Возвращает статус и список (уровень, что, что делать)."""
    items, water = [], []

    def add(level, text, todo):
        items.append((level, text, todo))

    o2, tw = s["o2"] or None, s["tw"] or None
    o2min, o2crit, tmin, tmax = s["o2min"], s["o2crit"], s["tmin"], s["tmax"]

    problems = []
    if tmin >= tmax:
        problems.append("нормы температуры заданы неверно")
        add("nd", "Нормы температуры заданы неверно: «от» должно быть меньше «до»",
            "Поправьте нормы в боковой панели.")
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
    if s["place"] != INDOOR and (s["wx"] == "гроза" or s["wind"] == "сильный"):
        add("warn", "Гроза или сильный ветер.",
            "Проверьте оборудование и безопасность людей.")

    levels = [lv for lv, _, _ in items]
    status = next((lv for lv in ("crit", "nd", "warn") if lv in levels), "ok")
    return dict(items=items, status=status, data_ok=data_ok, problems=problems, accident=accident,
                o2_crit=o2_crit, refusal=s["react"] == "отказывается", water=water)


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

    # Давление: поправка только при резком падении
    indoor = s["place"] == INDOOR
    other = s["react"] == "слабо" or s["left"] == "есть" or bool(ev["water"])
    out["press_would"] = (not indoor) and s["trend"] == "резко падает" and other
    out["press_on"] = out["press_would"] and bool(s["use_weather"])
    if why:
        return out

    rows = [
        ("Как рыба ест корм", s["c_ap"] if s["react"] == "слабо" else 1.0,
         "слабо" if s["react"] == "слабо" else "как обычно"),
        ("Остаток корма", s["c_lo"] if s["left"] == "есть" else 1.0,
         "корм остался" if s["left"] == "есть" else "корма не осталось"),
        ("Вода", s["c_wat"] if ev["water"] else 1.0,
         "; ".join(ev["water"]) or "кислород и температура в норме"),
    ]
    w, notes = 1.0, []
    if indoor:
        notes.append("бассейн в помещении")
    elif not s["use_weather"]:
        notes.append("учёт погоды выключен")
    else:
        if s["wx"] == "гроза" or s["wind"] == "сильный":
            w *= s["c_storm"]
            notes.append("гроза или сильный ветер")
        if out["press_on"]:
            w *= s["c_press"]
            notes.append("давление резко падает, проверить другие признаки")
        elif s["trend"] == "резко падает":
            notes.append("давление резко падает, но других признаков нет: порцию не меняем")
        if not notes:
            notes.append("погода хорошая")
    rows.append(("Погода", w, "; ".join(notes)))

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
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_record(kind, obj, kg=None, ref_id=None, comment="", payload=None):
    now = datetime.now()
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
        sql += " WHERE object_name=? AND local_date=?"
        args = (obj, datetime.now().strftime("%Y-%m-%d"))
    with db() as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    return [dict(zip(COLS, r)) for r in rows]


def payload_of(r):
    try:
        return json.loads(r["payload"] or "{}")
    except ValueError:
        return {}


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
        rows.append({"№": r["id"], "Время": r["local_time"], "Запись": name,
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
        row = [r["id"], r["local_date"], r["local_time"], r["object_name"],
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

DEMOS = {
    "Норма": dict(o2=8.0, tw=15.0, age=0, react="как обычно", left="нет", equip="работает"),
    "Отклонение": dict(o2=5.5, tw=19.0, age=0, react="слабо", left="нет", equip="работает"),
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
    ev = evaluate(S)
    return recs, rows, given, ev, suggest(S, ev, given)


def cb_demo(name):
    for k, v in DEMOS[name].items():
        S[k] = v
    if not S.daily:
        S.daily = 500.0
    if not S.feedings:
        S.feedings = 5
    flash("demo", f"Подставлен пример «{name}».", "info")


def cb_accept(kg):
    S.feed_kg = float(kg)
    flash("sug", "Объём подставлен в блок «Записать кормление». Пока это не запись.", "info")


def cb_feed():
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
    add_record("feeding", obj_name(), kg=round(kg, 2), comment=S.comment, payload=dict(
        suggested_kg=sg["final"] if sg["ok"] else None, action=action,
        adjustments="; ".join(f"{n}={fmt(v, 2)}" for n, v, _ in sg["rows"]),
        status=ev["status"], o2=S.o2, water_t=S.tw, reaction=S.react, leftovers=S.left,
        pressure=S.trend, weather_adjust_on=S.use_weather,
        pressure_adjust_applied=bool(sg["ok"] and sg["press_on"]),
        pressure_adjust_would_apply=sg["press_would"],
        daily_norm_kg=daily, one_feeding_kg=sg["base"], given_before_kg=given,
        over_daily_norm=bool(daily > 0 and given + kg > daily + 1e-9)))
    S.feed_kg = 0.0
    S.comment = ""
    flash("feed", f"Записано: скормлено {fmt(kg)} кг.")


def cb_reject():
    _, _, _, ev, sg = current_state()
    if not sg["ok"]:
        return
    add_record("rejected", obj_name(), comment=S.comment, payload=dict(
        suggested_kg=sg["final"], status=ev["status"],
        adjustments="; ".join(f"{n}={fmt(v, 2)}" for n, v, _ in sg["rows"]),
        pressure=S.trend, reaction=S.react, leftovers=S.left))
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
    st.selectbox("Тип", ["УЗВ", "Пруд", "Садки"], key="obj_type")
    st.text_input("Вид рыбы", key="species")
    st.selectbox("Где находится", [INDOOR, SHADE, OPEN], key="place")

    st.header("Нормы")
    st.caption("Значения примерные. Поставьте свои.")
    st.number_input("Кислород: норма от, мг/л", min_value=0.0, step=0.1, key="o2min")
    st.number_input("Кислород: критический уровень, мг/л", min_value=0.0, step=0.1, key="o2crit")
    st.number_input("Температура воды от, °C", step=0.5, key="tmin")
    st.number_input("Температура воды до, °C", step=0.5, key="tmax")
    st.number_input("Замер свежий, не старше, мин", min_value=0, step=1, key="maxage")

    with st.expander("Поправки к порции"):
        st.checkbox("Учитывать погоду и давление", key="use_weather")
        for _label, _key in [("Рыба берёт корм слабо", "c_ap"), ("Остался корм", "c_lo"),
                             ("Вода вне нормы", "c_wat"), ("Гроза или сильный ветер", "c_storm"),
                             ("Давление резко падает", "c_press")]:
            st.number_input(_label, min_value=0.0, max_value=1.0, step=0.05, key=_key)
        st.caption("1,0 — порция не меняется. Значения стартовые, на практике не проверены.")

# ---------- шапка ----------
st.title("Вишфиш")
st.caption("Прототип. Подсказывает по данным, которые вы вводите вручную. "
           "Решение по кормлению принимает рыбовод.")
with st.expander("Примеры для показа"):
    for _col, _name in zip(st.columns(4), DEMOS):
        _col.button(_name, key=f"demo_{_name}", on_click=cb_demo, args=(_name,))
    st.caption("Подставляют замеры. Если не указан корм на сутки, ставят 500 кг и 5 кормлений.")
    show_flash("demo")

# ---------- ввод ----------
c1, c2, c3 = st.columns(3)
with c1:
    st.subheader("Замеры")
    st.number_input("Кислород, мг/л", min_value=0.0, step=0.1, key="o2")
    st.number_input("Температура воды, °C", step=0.5, key="tw")
    st.caption("0 в этих полях — значит «не введено».")
    st.number_input("Замер сделан, мин назад", min_value=0, step=1, key="age")
    st.selectbox("Как рыба берёт корм", ["как обычно", "вяло", "отказывается"], key="react")
    st.selectbox("Остался ли корм после кормления", ["нет", "есть"], key="left")
    st.selectbox("Оборудование", ["работает", "авария"], key="equip")
with c2:
    st.subheader("Погода")
    if S.place == INDOOR:
        st.caption("Бассейн в помещении: погоду вводить не нужно.")
    else:
        st.selectbox("Погода", ["ясно", "облачно", "дождь", "гроза"], key="wx")
        st.selectbox("Ветер", ["слабый", "умеренный", "сильный"], key="wind")
    st.selectbox("Атмосферное давление", ["стабильное", "растёт", "падает", "резко падает"],
                 key="trend")
with c3:
    st.subheader("Корм на сегодня")
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
          "warn": ("Нужно обратить внимание", st.warning), "ok": ("Всё в норме", st.success)}
STATUS[ev["status"]][1](STATUS[ev["status"]][0])
if ev["status"] == "ok":
    st.caption("По введённым данным. Это не гарантия, что всё хорошо.")
for _lv, _text, _todo in ev["items"]:
    st.markdown(f"- **{_text}**  \n  Что делать: {_todo}")

# ---------- предложение ----------
st.subheader("Сколько дать на следующее кормление")
show_flash("sug")
if not sg["ok"]:
    st.warning("Предложения нет: " + "; ".join(sg["why"]) + ".")
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

_feeds = [r for r in recs if r["kind"] == "feeding"]
if _feeds:
    _ids = [r["id"] for r in _feeds]
    _names = {r["id"]: f"№{r['id']} · {r['local_time']} · {fmt(r['kg'])} кг" for r in _feeds}
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
