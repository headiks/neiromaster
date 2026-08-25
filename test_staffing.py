"""Тесты штатки: генерация логинов + единый разбор строк (без БД и без сети —
классификатор строк подменяем стабом)."""

import staffing


def test_username_base_surname_initials():
    assert staffing.username_base("Иванов Иван Иванович") == "ivanov_i_i"
    assert staffing.username_base("Петров Пётр") == "petrov_p"
    assert staffing.username_base("  Сидоров  ") == "sidorov"


def test_username_base_empty_and_short():
    assert staffing.username_base("") == "user"
    assert staffing.username_base("Ли") == "li0"


def test_username_base_non_cyrillic():
    assert staffing.username_base("Smith John") == "smith_j"


def test_unique_username_collision():
    taken = {"ivanov_i_i", "ivanov_i_i2"}
    assert staffing._unique_username("ivanov_i_i", taken) == "ivanov_i_i3"
    assert staffing._unique_username("petrov_p", taken) == "petrov_p"


def test_temp_password_length_and_charset():
    p = staffing._temp_password()
    assert len(p) >= 8
    assert not (set("0O1lI") & set(p))


def test_looks_like_name():
    assert staffing._looks_like_name("Иванов Иван Иванович")
    assert not staffing._looks_like_name("1")
    assert not staffing._looks_like_name("Кладовщик")
    assert not staffing._looks_like_name("Отдел 5")


def test_extract_unified_person_and_vacancy_with_banners():
    # смешанная таблица: шапка, баннер-отдел (col0), человек, ещё баннер, вакансия, итог
    grid = [
        ["№", "Сотрудник", "Должность", "Дата"],
        ["Администрация", "", "", ""],                                    # баннер отдела
        ["1", "Иванов Иван Иванович", "Бухгалтер", "06.02.2026"],         # человек
        ["Отдел продаж", "", "", ""],                                     # баннер отдела
        ["2", "", "Менеджер", ""],                                        # вакансия (ФИО нет)
        ["Итого", "", "", ""],                                            # мусор
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": 1, "position": 2, "department": None, "start_date": 3},
               "sections": {"department": 0}}
    # стаб-классификатор: отделы -> org, должность -> job
    stub = staffing.make_unit_classifier(llm=lambda t: "org" if "Отдел" in t or "Администрация" in t else "job")
    recs = staffing.extract_unified(grid, mapping, classifier=stub)
    assert recs == [
        {"full_name": "Иванов Иван Иванович", "position": "Бухгалтер", "department": "Администрация", "start_date": "06.02.2026"},
        {"full_name": "", "position": "Менеджер", "department": "Отдел продаж", "start_date": ""},
    ]


def test_extract_unified_position_column_is_banner():
    # ШР-стиль: должности и подразделения в ОДНОМ столбце (col0)
    grid = [
        ["Должность", "Ставок"],
        ["Обособленное подразделение А", "1"],   # баннер (org по ключевому слову)
        ["Ведущий инженер", "1"],                # должность-вакансия
        ["Кладовщик", "8"],                      # должность-вакансия
    ]
    mapping = {"data_start_row": 1,
               "columns": {"full_name": None, "position": 0, "department": None, "start_date": None},
               "sections": {"department": 0}}
    recs = staffing.extract_unified(grid, mapping)   # «подразделение» ловится ключевым словом, без сети
    assert [r["position"] for r in recs] == ["Ведущий инженер", "Кладовщик"]
    assert all(r["department"] == "Обособленное подразделение А" for r in recs)
    assert all(r["full_name"] == "" for r in recs)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("ALL PASS")
