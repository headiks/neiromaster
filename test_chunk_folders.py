"""Тест разметки чанка по папкам: чанк не уходит в чужую папку и не наследует все
папки документа — только пересечение (папки документа) ∩ (близкие папке чанки).
Чистая логика, без Qdrant/БД."""

from classify import select_chunk_folders


def _m(*pairs):
    # (slug, name, stage_ids, score) — как отдаёт match_folders
    return [(slug, slug, [], score) for slug, score in pairs]


def test_intersection_only():
    doc = ["fire_safety", "ppe"]
    matches = _m(("fire_safety", 0.7), ("welding", 0.9), ("ppe", 0.5))
    # welding у чанка сильный, но документ к нему не отнесён -> не берём (анти-утечка)
    assert select_chunk_folders(matches, doc) == ["fire_safety", "ppe"]


def test_no_inherit_when_no_match():
    # чанк ни к одной папке документа не близок -> пусто (уйдёт только в общую базу),
    # а не наследует все папки документа
    assert select_chunk_folders(_m(("welding", 0.8)), ["fire_safety", "ppe"]) == []


def test_subset_of_doc():
    # берём только ту папку документа, к которой чанк реально близок
    assert select_chunk_folders(_m(("ppe", 0.6)), ["fire_safety", "ppe"]) == ["ppe"]


def test_empty_inputs():
    assert select_chunk_folders([], ["a"]) == []
    assert select_chunk_folders(_m(("a", 0.9)), []) == []


def test_no_duplicates():
    assert select_chunk_folders(_m(("a", 0.9), ("a", 0.8)), ["a"]) == ["a"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("ALL PASS")
