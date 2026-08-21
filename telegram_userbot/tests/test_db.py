"""
Oddiy unit testlar. Ishga tushirish:
    pip install pytest --break-system-packages
    pytest tests/

To'liq end-to-end test (haqiqiy Telegram bilan) yozib bo'lmaydi, chunki bu
haqiqiy akkaunt va tarmoqni talab qiladi. Shu sabab bu yerda faqat tarmoqqa
bog'liq bo'lmagan sof mantiq (db.py) test qilinadi.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_db(tmp_path):
    os.environ["API_ID"] = "1"
    os.environ["API_HASH"] = "x"
    os.environ["PHONE"] = "+10000000000"
    os.environ["BOT_TOKEN"] = "x"
    os.environ["OWNER_ID"] = "1"
    os.environ["DB_PATH"] = str(tmp_path / "test.db")

    import config
    importlib.reload(config)
    import db
    importlib.reload(db)
    db.init_db()
    return db


def test_toggle_selected(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    assert db.get_selected(owner) == {}

    added = db.toggle_selected(owner, 100, "Test Group")
    assert added is True
    assert db.get_selected(owner) == {100: "Test Group"}

    removed = db.toggle_selected(owner, 100, "Test Group")
    assert removed is False
    assert db.get_selected(owner) == {}


def test_bulk_select_and_clear(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    db.add_selected_bulk(owner, [(1, "A"), (2, "B"), (3, "C")])
    assert len(db.get_selected(owner)) == 3
    db.remove_selected(owner, 2)
    assert len(db.get_selected(owner)) == 2
    db.clear_selected(owner)
    assert db.get_selected(owner) == {}


def test_confirm_token_is_single_use(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    db.set_confirm(owner, "abc123", "clearall", ttl_seconds=60)

    # To'g'ri token, to'g'ri action -> bir marta ishlaydi
    ok, _ = db.check_and_consume_confirm(owner, "abc123", "clearall")
    assert ok is True
    # Xuddi shu tokenni ikkinchi marta ishlatishga urinish -> rad etiladi
    ok, _ = db.check_and_consume_confirm(owner, "abc123", "clearall")
    assert ok is False


def test_confirm_token_wrong_action_rejected(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    db.set_confirm(owner, "abc123", "clear", ttl_seconds=60)
    ok, _ = db.check_and_consume_confirm(owner, "abc123", "clearall")
    assert ok is False


def test_confirm_token_expired_rejected(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    db.set_confirm(owner, "abc123", "clear", ttl_seconds=-1)  # darhol muddati o'tadi
    ok, _ = db.check_and_consume_confirm(owner, "abc123", "clear")
    assert ok is False


def test_confirm_token_carries_target_snapshot(tmp_path):
    """Tasdiqlash so'ralgandan keyin tanlov o'zgarsa ham, tasdiqlangan amal
    ANA SHU eski (snapshot) chatlarga tegishi kerak — yangi tanlovga emas."""
    db = _fresh_db(tmp_path)
    owner = 1
    snapshot = [[100, "Group A"], [200, "Group B"]]
    db.set_confirm(owner, "abc123", "clear", ttl_seconds=60, targets=snapshot)

    # Tasdiqlash oynasi ochiq turgan paytda foydalanuvchi tanlovni o'zgartiradi:
    db.toggle_selected(owner, 300, "Group C")

    ok, targets = db.check_and_consume_confirm(owner, "abc123", "clear")
    assert ok is True
    assert targets == snapshot  # yangi tanlangan 300 emas, eski snapshot qaytadi


def test_awaiting_state(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    assert db.get_awaiting(owner) is None
    db.set_awaiting(owner, "send_content")
    assert db.get_awaiting(owner) == "send_content"
    db.set_awaiting(owner, None)
    assert db.get_awaiting(owner) is None


def test_awaiting_targets_snapshot(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    assert db.get_awaiting_targets(owner) is None
    db.set_awaiting(owner, "send_content", targets=[[1, "A"], [2, "B"]])
    assert db.get_awaiting_targets(owner) == [[1, "A"], [2, "B"]]
    db.set_awaiting(owner, None)
    assert db.get_awaiting_targets(owner) is None


def test_search_results_cache(tmp_path):
    db = _fresh_db(tmp_path)
    owner = 1
    assert db.get_search_results(owner, "groups") is None
    matches = [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Alphabet"}]
    db.set_search_results(owner, "groups", matches)
    assert db.get_search_results(owner, "groups") == matches
    # Boshqa kategoriya bo'yicha so'ralsa, mos kelmagani uchun None qaytishi kerak
    assert db.get_search_results(owner, "channels") is None


def test_search_caches_are_independent_by_category(tmp_path):
    db = _fresh_db(tmp_path)
    db.set_search_results(1, "groups", [{"id": 10, "name": "G"}])
    db.set_search_results(1, "channels", [{"id": 20, "name": "C"}])
    assert db.get_search_results(1, "groups")[0]["id"] == 10
    assert db.get_search_results(1, "channels")[0]["id"] == 20


def test_clear_selected_only_removes_snapshot(tmp_path):
    db = _fresh_db(tmp_path)
    db.add_selected_bulk(1, [(1, "Old"), (2, "Keep")])
    # A new selection made while deletion is running must survive.
    db.add_selected_bulk(1, [(3, "New")])
    db.clear_selected(1, [1])
    assert db.get_selected(1) == {2: "Keep", 3: "New"}


def test_audit_log_is_persistent(tmp_path):
    db = _fresh_db(tmp_path)
    db.add_audit_log(1, "broadcast", {"cancelled": True, "success": 2})
    row = db.get_audit_log(1, 1)[0]
    assert row[0] == "broadcast"
