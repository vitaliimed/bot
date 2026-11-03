import unittest
import asyncio
import os
from unittest.mock import patch
from app.ua_language_guard import is_ukrainian_body, ukrainian_required, _small_numbers, protect_publish

class UkrainianGuardTests(unittest.TestCase):
    def test_finnish_source_blocked(self):
        self.assertFalse(is_ukrainian_body("Varmista soittoajat keikkapäivänä Tavastian sivuilta. Britti-indien suosikki palaa Suomeen Helsingin Tavastialle sunnuntaina."))

    def test_english_source_blocked(self):
        self.assertFalse(is_ukrainian_body("The concert is scheduled in Helsinki on Sunday and tickets start from thirty-one euros."))

    def test_ukrainian_with_artist_and_address_allowed(self):
        self.assertTrue(is_ukrainian_body("Британський гурт Razorlight виступить 25 жовтня в клубі Tavastia у Гельсінкі. Початок о 18:00, квитки коштують від 31 євро."))

    def test_empty_body_blocked(self):
        self.assertFalse(is_ukrainian_body(""))

    def test_target_language_required(self):
        self.assertTrue(ukrainian_required("українська"))
        self.assertTrue(ukrainian_required("українська + suomi"))
        self.assertFalse(ukrainian_required("suomi"))

    def test_extra_numeric_facts(self):
        self.assertTrue(_small_numbers("32 €") - _small_numbers("31 €"))

class FakeSnapshot:
    exists = True
    def __init__(self, item): self.item = item
    def to_dict(self): return dict(self.item)

class FakeRef:
    def __init__(self, item): self.item = dict(item); self.updates = []
    def get(self): return FakeSnapshot(self.item)
    def update(self, data): self.updates.append(data); self.item.update(data)

class FakeDb:
    def __init__(self, ref): self.ref = ref
    def collection(self, name):
        assert name == "items"
        return self
    def document(self, docid): return self.ref

class IntegrationTests(unittest.TestCase):
    def test_blocks_finnish_before_original_publisher(self):
        original_calls = []
        async def original(docid, automatic=False, cfg=None):
            original_calls.append(docid)
            return True
        async def editor(route): return {"language": "українська"}
        ref = FakeRef({"route": "events", "status": "pending", "title": "Razorlight", "text": "Varmista soittoajat keikkapäivänä Tavastian sivuilta."})
        namespace = {"db": FakeDb(ref), "editor_config": editor, "route_of": lambda item: item["route"]}
        fn = protect_publish(original, namespace)
        with patch.dict(os.environ, {"UA_TRANSLATION_MODE": "off"}):
            result = asyncio.run(fn("1", automatic=True))
        self.assertFalse(result)
        self.assertEqual(original_calls, [])
        self.assertEqual(ref.item["language_guard_status"], "blocked")

    def test_publishes_ukrainian_via_original_publisher(self):
        original_calls = []
        async def original(docid, automatic=False, cfg=None):
            original_calls.append(docid)
            return True
        async def editor(route): return {"language": "українська"}
        ref = FakeRef({"route": "events", "status": "pending", "text": "Британський гурт Razorlight виступить у Гельсінкі. Квитки вже можна придбати на офіційному сайті концертного клубу."})
        fn = protect_publish(original, {"db": FakeDb(ref), "editor_config": editor, "route_of": lambda item: item["route"]})
        self.assertTrue(asyncio.run(fn("1", automatic=True)))
        self.assertEqual(original_calls, ["1"])
        self.assertEqual(ref.updates, [])

if __name__ == "__main__":
    unittest.main()
