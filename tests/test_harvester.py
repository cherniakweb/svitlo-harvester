import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dtek_harvester as h

FIX = (Path(__file__).parent / "fixture_2026-11-06.html").read_text(encoding="utf-8")


class Parse(unittest.TestCase):
    def test_real_page_31(self):
        p = h.parse(FIX, "3.1")
        # matches the DTEK table for 06.11 and 07.11 (key "1" = 00:00–01:00; first/second = half hours)
        self.assertEqual(p["days"]["2026-11-06"],
                         [["00:30", "04:30"], ["07:30", "09:30"], ["11:00", "18:30"], ["21:30", "24:00"]])
        self.assertEqual(p["days"]["2026-11-07"],
                         [["00:00", "05:00"], ["08:00", "15:30"], ["18:30", "24:00"]])
        self.assertEqual(p["updated_at"], "2026-11-06T23:23:00+02:00")
        self.assertEqual(p["unknown_values"], [])

    def test_other_group(self):
        self.assertEqual(h.parse(FIX, "3.2")["days"]["2026-11-06"][1], ["06:00", "08:00"])

    def test_missing_group_day_is_null(self):
        with self.assertRaises(ValueError):
            h.parse("no data", "3.1")
        p = h.parse(FIX, "6.2")
        self.assertEqual(p["days"], {"2026-11-06": None, "2026-11-07": None})

    def test_all_yes_is_empty_list_and_maybe(self):
        hours = {str(i): "yes" for i in range(1, 25)}
        self.assertEqual(h.day_intervals(hours, h.OFF), [])
        hours["10"] = "mfirst"; hours["11"] = "maybe"; hours["12"] = "maybe"
        self.assertEqual(h.day_intervals(hours, h.MAYBE), [["09:00", "09:30"], ["10:00", "12:00"]])
        self.assertEqual(h.day_intervals(hours, h.OFF), [])   # "maybe" never counts as a planned outage

    def test_schedule_doc_passes_worker_validation_shape(self):
        doc = h.build_doc(h.parse(FIX, "3.1"), {"group": "3.1", "url": "u", "keep_days": 7000}, None)
        for k, v in doc["days"].items():
            for a, b in v:
                self.assertLess(h_to(a), h_to(b))


def h_to(s):
    x, y = s.split(":"); return int(x) * 60 + int(y)


class Emergency(unittest.TestCase):
    def test_real_notice(self):
        t = h.notice_text(FIX)
        self.assertIn("Укренерго", t)
        self.assertTrue(h.is_emergency(t))

    def test_cancelled_and_absent(self):
        self.assertFalse(h.is_emergency("Екстрені відключення скасовано. Графіки відключень не діють до 18:00"))
        self.assertFalse(h.is_emergency("Діють графіки погодинних відключень"))
        self.assertFalse(h.is_emergency(""))
        self.assertEqual(h.notice_text("<html>nothing</html>"), "")


class Merge(unittest.TestCase):
    def test_keeps_previous_days(self):
        prev = {"days": {"2026-11-05": [["10:00", "12:00"]], "2026-11-06": [["00:00", "01:00"]]}}
        cfg = {"group": "3.1", "url": "u", "keep_days": 7000}
        doc = h.build_doc(h.parse(FIX, "3.1"), cfg, prev)
        self.assertEqual(list(doc["days"]), ["2026-11-05", "2026-11-06", "2026-11-07"])
        self.assertEqual(doc["days"]["2026-11-06"][0], ["00:30", "04:30"])  # fresh data wins


if __name__ == "__main__":
    unittest.main()
