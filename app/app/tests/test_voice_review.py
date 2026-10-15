"""План разбора голосов (scripts/voice_review.py): решения → шаги. Имена вымышленные."""
from scripts.voice_review import plan

DATA = {"voices": [{"id": 1, "name": "Игорь Пилот"}, {"id": 2, "name": "Игорь"}, {"id": 3, "name": "Пилот-2"},
                   {"id": 4, "name": "Олег Петров"}]}


def test_pairs_and_merges_resolve_to_one_keeper():
    steps = plan({"pairs": {"1:2": "keep_a", "1:4": "different"}, "voices": {"3": {"verdict": "merge", "merge_into": 2}}}, DATA)
    merges = {(s["dup"], s["keep"]) for k, s in steps if k == "merge"}
    assert merges == {(2, 1), (3, 1)}  # 3 → 2 → 1: цепочка сворачивается к оставляемому


def test_delete_of_merged_voice_is_flagged_not_done():
    steps = plan({"pairs": {"1:3": "keep_a"}, "voices": {"3": {"verdict": "delete"}}}, DATA)
    kinds = [k for k, _ in steps]
    assert "delete" not in kinds and "note" in kinds


def test_rename_and_person_fields():
    steps = plan({"voices": {"4": {"verdict": "rename", "name": "Олег Петров-Водкин", "company": "Пример", "side": "customer"}}}, DATA)
    assert [k for k, _ in steps] == ["rename", "person"]
    assert steps[1][1]["side"] == "customer" and steps[1][1]["id"] == 4


def test_unsure_and_empty_do_nothing():
    assert plan({"voices": {"1": {"verdict": "unsure"}, "2": {}}, "pairs": {"1:2": "unsure"}}, DATA) == []
