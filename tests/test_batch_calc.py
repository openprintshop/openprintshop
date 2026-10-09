"""Пакетный расчёт ссылок (OpenTodo 89251db6): извлечение ссылок и форма статуса."""
from app import webapp


def test_extract_links_dedup_and_trim():
    text = ("Привет! Вот: https://www.printables.com/model/123-x, ещё "
            "https://makerworld.com/en/models/5#profileId-9 и снова "
            "https://www.printables.com/model/123-x — посчитай (https://thingiverse.com/thing:7).")
    links = webapp._extract_links(text)
    assert links == [
        "https://www.printables.com/model/123-x",
        "https://makerworld.com/en/models/5#profileId-9",
        "https://thingiverse.com/thing:7",
    ]


def test_extract_links_empty_and_cap():
    assert webapp._extract_links("без ссылок совсем") == []
    assert webapp._extract_links("") == []
    many = " ".join(f"https://e.com/{i}" for i in range(30))
    assert len(webapp._extract_links(many)) == webapp._BATCH_MAX_LINKS


def test_batch_json_shape():
    b = {"user_id": 42, "user_name": "Аня", "done": False,
         "items": [{"url": "https://e.com/1", "status": "ok", "label": "Деталь", "price": 5.5,
                    "grams": 12.3, "minutes": 90, "plastic_name": "PLA", "plates": 1,
                    "preview": "C:/missing.png"},
                   {"url": "https://e.com/2", "status": "err", "error": "капча"}]}
    j = webapp._batch_json("abc", b)
    assert j["ok"] and j["user_id"] == 42 and j["done"] is False
    ok, err = j["items"]
    assert ok["price"] == 5.5 and ok["preview"].startswith("/batchprev/abc/0?s=")
    assert err["status"] == "err" and err["error"] == "капча" and "price" not in err
