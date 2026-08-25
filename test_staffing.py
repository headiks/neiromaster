"""Тест генерации логинов из ФИО (чистая логика, без БД)."""

import staffing


def test_username_base_surname_initials():
    assert staffing.username_base("Иванов Иван Иванович") == "ivanov_i_i"
    assert staffing.username_base("Петров Пётр") == "petrov_p"
    assert staffing.username_base("  Сидоров  ") == "sidorov"


def test_username_base_empty_and_short():
    assert staffing.username_base("") == "user"
    assert staffing.username_base("Ли") == "li0"     # < 3 -> дополняем


def test_username_base_non_cyrillic():
    assert staffing.username_base("Smith John") == "smith_j"


def test_unique_username_collision():
    taken = {"ivanov_i_i", "ivanov_i_i2"}
    assert staffing._unique_username("ivanov_i_i", taken) == "ivanov_i_i3"
    assert staffing._unique_username("petrov_p", taken) == "petrov_p"


def test_temp_password_length_and_charset():
    p = staffing._temp_password()
    assert len(p) >= 8
    assert not (set("0O1lI") & set(p))   # без похожих символов


def test_looks_like_name():
    assert staffing._looks_like_name("Иванов Иван Иванович")
    assert not staffing._looks_like_name("1")
    assert not staffing._looks_like_name("Кладовщик")      # одно слово, без пробела
    assert not staffing._looks_like_name("Отдел 5")        # есть цифра


def test_is_roster_vs_schedule():
    roster = [{"full_name": "Иванов Иван Иванович"}, {"full_name": "Петров Пётр Петрович"}]
    schedule = [{"full_name": "1"}, {"full_name": "8"}, {"full_name": ""}]
    assert staffing._is_roster(roster) is True
    assert staffing._is_roster(schedule) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("ALL PASS")
