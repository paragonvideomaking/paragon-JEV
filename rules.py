"""Логика статусов, порогов и шаблонов для демо «ТЗ перед стартом».

Все функции здесь чистые и не выполняют сетевых запросов, поэтому их
можно проверять локальными тестами (tests/test_rules.py).

ВАЖНО про пороги: значения ниже — УЧЕБНЫЕ, выбраны для демонстрации, а не
рекомендация производителя модели. Меняйте их под свою задачу и данные.
"""

# --- Учебные пороги (не рекомендации производителя) ---

# Порог уверенности (confidence) для пяти вопросов-Choice.
# Ниже порога итог строки — «Проверить вручную».
CHOICE_CONFIDENCE_THRESHOLD = 0.6  # учебный порог

# Пороги для Noul (has_testable_acceptance).
NOUL_NO_MAX = 0.2   # <= 0.2 — «нет» (учебный порог)
NOUL_YES_MIN = 0.8  # >= 0.8 — «да» (учебный порог)

# Человеко-читаемые метки статуса строки Choice.
CHOICE_LABELS = {
    "present": "Есть",
    "vague": "Расплывчато",
    "missing": "Нет",
}

MANUAL_LABEL = "Проверить вручную"

# Человеко-читаемые названия критериев для отчёта.
CRITERION_TITLES = {
    "result": "Результат",
    "scope": "Объём работ",
    "constraints": "Ограничения",
    "deadline": "Срок",
    "acceptance": "Приёмка",
}

# Цветовые коды статусов (текст + цвет). Значения — семантические имена,
# которые фронтенд отображает как классы, не полагаясь только на цвет.
STATUS_TONE = {
    "Есть": "good",
    "Расплывчато": "warn",
    "Нет": "bad",
    MANUAL_LABEL: "manual",
}

# Шаблоны уточняющих вопросов по ID. Это подготовленные тексты чек-листа,
# а НЕ сгенерированное моделью объяснение и не цитаты из ТЗ.
QUESTION_TEMPLATES = {
    "result": "Какой конкретный результат и в каком формате нужно передать?",
    "scope": "Что входит в работу, а что явно исключено?",
    "constraints": (
        "Какие технологии, материалы и другие ограничения нужно соблюдать? "
        "Если ограничений нет, укажите это явно."
    ),
    "deadline": (
        "К какой дате и времени нужен результат? Для относительного срока "
        "укажите точку отсчёта."
    ),
    "acceptance": "Какими конкретными проверками вы будете принимать результат?",
}

NOUL_YES = "да"
NOUL_NO = "нет"
NOUL_UNCERTAIN = "неопределённо"

ACCEPTANCE_MANUAL_WARNING = "Приёмку проверить вручную"


def classify_noul(noul_value):
    """Классифицировать значение Noul по учебным порогам."""
    if noul_value <= NOUL_NO_MAX:
        return NOUL_NO
    if noul_value >= NOUL_YES_MIN:
        return NOUL_YES
    return NOUL_UNCERTAIN


def derive_choice_row(qid, choice, confidence):
    """Построить строку отчёта по одному вопросу-Choice.

    Возвращает dict со статусом, тоном, уверенностью, флагом ручной проверки
    и исходным выбором модели.
    """
    manual = confidence < CHOICE_CONFIDENCE_THRESHOLD
    if manual:
        status = MANUAL_LABEL
    else:
        status = CHOICE_LABELS.get(choice, choice)

    return {
        "id": qid,
        "title": CRITERION_TITLES.get(qid, qid),
        "status": status,
        "tone": STATUS_TONE.get(status, "manual"),
        "confidence": confidence,
        "manual": manual,
        "raw_choice": choice,
        "raw_choice_label": CHOICE_LABELS.get(choice, choice),
    }


def needs_clarifying_question(row):
    """Нужен ли уточняющий вопрос для строки Choice.

    Не нужен только для уверенного «present» (Есть без сомнений).
    Нужен для vague, missing и для ручной проверки.
    """
    if row["manual"]:
        return True
    return row["raw_choice"] in ("vague", "missing")


def build_clarifying_questions(rows):
    """Собрать список уточняющих вопросов без дублей, в порядке критериев."""
    questions = []
    seen = set()
    for row in rows:
        if not needs_clarifying_question(row):
            continue
        template = QUESTION_TEMPLATES.get(row["id"])
        if template and template not in seen:
            seen.add(template)
            questions.append(template)
    return questions


def acceptance_conflict(rows, noul_class):
    """Есть ли спорная приёмка.

    Спорно, если Noul неопределён, либо Noul и вывод по критерию acceptance
    противоречат друг другу (например, приёмка «Есть», но Noul говорит «нет»,
    или наоборот).
    """
    acc_row = next((r for r in rows if r["id"] == "acceptance"), None)
    if noul_class == NOUL_UNCERTAIN:
        return True
    if acc_row is None:
        return False

    if acc_row["manual"]:
        # Критерий приёмки на ручной проверке, но Noul дал однозначный ответ —
        # это расхождение источников, помечаем как спорное.
        return True

    acc_has = acc_row["raw_choice"] == "present"
    noul_has = noul_class == NOUL_YES
    return acc_has != noul_has


def build_report(answers):
    """Построить полный отчёт из ответов API (уже провалидированных).

    `answers` — dict с семью ключами из questions.QUESTION_IDS.
    Возвращает структуру для отправки клиенту (без секретов).
    """
    rows = []
    for qid in ("result", "scope", "constraints", "deadline", "acceptance"):
        ans = answers[qid]
        rows.append(
            derive_choice_row(qid, ans["choice"], ans["confidence"])
        )

    # Score: ясность результата. Показываем дробное значение и легенду как есть.
    score_ans = answers["result_clarity"]
    result_clarity = {
        "score": score_ans["score"],
        "legend": score_ans["legend"],
        "probabilities": score_ans.get("probabilities", {}),
        "confidence": score_ans.get("confidence"),
    }

    # Noul: наличие проверяемой приёмки.
    noul_value = answers["has_testable_acceptance"]["noul"]
    noul_class = classify_noul(noul_value)
    acceptance_noul = {
        "noul": noul_value,
        "verdict": noul_class,
    }

    conflict = acceptance_conflict(rows, noul_class)

    warnings = []
    if conflict:
        warnings.append(ACCEPTANCE_MANUAL_WARNING)

    clarifying = build_clarifying_questions(rows)

    summary = build_summary(rows, noul_class, conflict)

    return {
        "rows": rows,
        "result_clarity": result_clarity,
        "acceptance_noul": acceptance_noul,
        "acceptance_conflict": conflict,
        "warnings": warnings,
        "clarifying_questions": clarifying,
        "summary": summary,
        "disclaimer": (
            "Это проверка по заданному чек листу, не гарантия выполнимости "
            "или полного качества ТЗ."
        ),
    }


def build_summary(rows, noul_class, conflict):
    """Определить итоговый вердикт.

    Приоритет:
      1) Если есть неопределённость (ручная проверка строк или спорная
         приёмка/неопределённый Noul) — «Нужна ручная проверка».
      2) Иначе, если есть vague или missing — «Нужно уточнить».
      3) Иначе — «По пяти критериям пробелы не выявлены».

    Score не влияет на итог. Отчёт со спорной/неопределённой приёмкой
    не может получить общий зелёный статус.
    """
    any_manual = any(r["manual"] for r in rows)
    uncertainty = any_manual or conflict or noul_class == NOUL_UNCERTAIN

    if uncertainty:
        text = "Нужна ручная проверка"
        tone = "manual"
    else:
        has_gap = any(r["raw_choice"] in ("vague", "missing") for r in rows)
        if has_gap:
            text = "Нужно уточнить"
            tone = "warn"
        else:
            text = "По пяти критериям пробелы не выявлены"
            tone = "good"

    return {"text": text, "tone": tone}
