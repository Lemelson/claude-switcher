#!/usr/bin/env python3
"""Checks of the overview grid and provider caches."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import calib
import core
import split_attrib as sa
import tick
import timeline


class TimelineCheck(unittest.TestCase):
    def test_system_python_can_import_collector(self):
        result = subprocess.run(
            ["/usr/bin/python3", "-c", "import sys; sys.path.insert(0, 'usage'); import tick, split_attrib, timeline"],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_grid_keeps_observed_increments_across_resets(self):
        now = 1795861532
        start = now - 86400
        account = {
            "meta": {"key": "codex:a", "provider": "codex", "usdPer100": None, "quotaThrough": now, "rated": False},
            "quota": [[start, start + 900, 0, 12, 0, 0],
                      [start + 900, start + 1800, 0, 0, 0, 16],
                      [start + 1800, start + 3600, 0, 3, 2, 0]],
            "spend": [[start + 900, 4.125, 90]],
        }
        cache = {"builtAt": now, "partial": False, "accounts": [account],
                 "unattributed": [[start + 3600, 1.25, 10]]}
        got = timeline.overview({"codex": cache}, now)
        self.assertEqual([len(r["combined"]["quota"]) for r in got["ranges"].values()], [97, 145, 169])
        span = got["ranges"]["24h"]
        rows = span["accounts"]["codex:a"]["quota"]
        self.assertEqual(rows[0], [0, 0, 0, 0])
        self.assertEqual(rows[4][:3], [0, 15, 2])
        self.assertEqual(span["combined"]["quota"][4][:3], [0, .15, .02])
        self.assertEqual(span["combined"]["unratedAccounts"], ["codex:a"])
        self.assertEqual(span["combined"]["unknownAccounts"], ["codex:a"])
        self.assertEqual(span["combined"]["totalYouUsd"], 5.38)
        self.assertEqual(span["combined"]["totalYouTokens"], 100)

    def test_confirmed_and_scheduled_resets_do_not_drop_the_curve(self):
        start = 1795700000
        now = start + 86400
        reset = start + 3600
        credit = start + 1800
        points = [(start - 1800, 40, reset), (start + 900, 70, reset),
                  (start + 2700, 80, reset), (start + 4500, 9, reset + 604800),
                  (start + 5400, 13, reset + 604800)]
        attrib = sa.attribute(points, [], "7d", 1, resets=[credit])
        account = timeline.account_input("codex", {"uuid": "a"}, attrib.intervals, [], now)
        cache = timeline.provider_input(now, [account])
        series = timeline.overview({"codex": cache}, now)["ranges"]["24h"]["accounts"]["codex:a"]["quota"]
        self.assertEqual(series[1][:3], [0, 0, 0])
        self.assertTrue(series[1][3] & sa.BOUNDARY)
        self.assertEqual(series[3][:3], [0, 80, 0])
        self.assertTrue(series[3][3] & sa.RESET_TAIL)
        self.assertEqual(series[5][:3], [0, 89, 0])
        self.assertEqual(series[6][:3], [0, 93, 0])
        self.assertTrue(all(a[1] <= b[1] for a, b in zip(series, series[1:])))

    def test_claude_weekly_hits_and_fable_weight(self):
        now = 1795861532
        start = now - 86400
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(core.SCHEMA)
        reset = start + calib.SPAN["7d"]
        con.executemany("INSERT INTO samples VALUES(?,?,?,?,?,?)",
                        [("a", "7d", start, 0, reset, "poll"), ("a", "7d", start + 180, 30, reset, "poll")])
        con.execute("INSERT INTO hits(key,ts,kind,resets_at,acc) VALUES(?,?,?,?,?)",
                    ("hit", start + 600, "seven_day", reset, "a"))
        con.executemany("INSERT INTO rows(key,ts,model,cost,inp,acc) VALUES(?,?,?,?,?,?)",
                        [("fable", start + 120, "claude-fable-5", 4, 100, "a"),
                         ("no-account", start + 720, "claude-opus-5", 5, 200, None)])
        info = {"uuid": "a", "kFable": 3, "rates": {"7d": {"pct_per_usd": 2, "usd_per_100": 50}}}
        res = timeline.overview({"claude": timeline.claude_input(con, [info], now)}, now)["ranges"]["24h"]
        quota = res["accounts"]["claude:a"]["quota"]
        self.assertEqual(quota[1][:3], [24, 76, 0])
        self.assertTrue(quota[1][3] & sa.CALIBRATED)
        self.assertTrue(quota[1][3] & sa.RESET_TAIL)
        self.assertEqual(res["accounts"]["claude:a"]["usdYou"][-1], 4)
        self.assertEqual(res["combined"]["totalYouUsd"], 9)
        self.assertEqual(res["combined"]["totalOthersUsd"], 38)
        self.assertEqual(res["combined"]["totalYouTokens"], 300)

    def test_switch_refreshes_only_codex_and_does_not_count_its_top_level_block(self):
        now = 1795861532
        start = now - 86400
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with patch.multiple(core, DATA=str(data), DB=str(data / "usage.db")):
                con = core.db()
                self.addCleanup(con.close)
                for provider, dollars in (("claude", 5), ("antigravity", 8), ("codex", 2)):
                    acct = timeline.account_input(provider, {"uuid": provider}, [], [(start + 60, dollars, 2)], now - 10)
                    core.kv_set(con, f"timeline_provider:{provider}", timeline.provider_input(now - 10, [acct]))
                con.commit()
                keys = ["timeline_provider:claude", "timeline_provider:antigravity"]
                cached = [core.kv_get(con, key) for key in keys]
                state_file = data / "state.json"
                state_file.write_text(json.dumps({"schema": 4, "generatedAt": now - 10, "accounts": [{"uuid": "claude"}],
                                                  "claude24h": {"usd": 5}, "antigravity": {"usd24h": 8}}))

                def fresh_codex(con, ts, *, timeline_inputs):
                    blocks = [{"uuid": "local", "usd24h": 7}, {"uuid": "shared", "usd24h": 0}]
                    entries = [
                        timeline.account_input("codex", blocks[0], [[start, start + 900, 4, 0, 0, 0]],
                                               [(start + 60, 7, 10)], ts),
                        timeline.account_input("codex", blocks[1], [[start, start + 900, 0, 12, 1, 0]], [], ts),
                    ]
                    timeline_inputs["codex"] = timeline.provider_input(ts, entries, [(start + 60, 3, 5)])
                    return {"uuid": "codex", "usd24h": 10, "accounts": blocks}, {}

                with (patch.object(sys, "argv", ["tick.py", "--switch", "--provider", "codex"]),
                      patch.object(tick.time, "time", return_value=now),
                      patch.object(tick.codex, "block", side_effect=fresh_codex),
                      patch.object(tick.codex, "import_quota", return_value={}),
                      patch.object(tick.antigravity, "block", side_effect=AssertionError("Antigravity rebuild")),
                      patch.object(tick.calib, "analyse", side_effect=AssertionError("Claude rebuild")),
                      patch.object(tick.poll, "poll", side_effect=AssertionError("Claude poll")),
                      patch.object(tick.scan, "scan", side_effect=AssertionError("Claude scan")),
                      patch("report.write")):
                    tick.main()

                st = json.loads(state_file.read_text())
                self.assertEqual(st["schema"], 4)
                overview = st["overviewTimeline"]
                self.assertEqual(overview["asOf"], now)
                self.assertEqual([core.kv_get(con, key) for key in keys], cached)
                self.assertEqual([p["builtAt"] for p in overview["providers"].values()], [now - 10, now, now - 10])
                self.assertEqual([a["key"] for a in overview["accounts"]],
                                 ["claude:claude", "codex:local", "codex:shared", "antigravity:antigravity"])
                combined = overview["ranges"]["24h"]["combined"]
                self.assertEqual(combined["quota"][-1][:3], [.04, .12, .01])
                self.assertEqual(combined["totalYouUsd"], st["spend24h"]["usd"])
                self.assertTrue(st["spend24h"]["partial"])
                self.assertEqual(combined["totalYouTokens"], 19)
                self.assertFalse((data / "state.json.tmp").exists())

    def test_invalid_cache_does_not_hide_other_providers(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(core.SCHEMA)
        now = 1795861532
        codex = timeline.provider_input(now, [
            timeline.account_input("codex", {"uuid": "rated", "rates": {"7d": {"usd_per_100": 50}}},
                                   [[now - 300, now, 0, 10, 4, 0]], [(now - 60, 7, 10)], now),
            timeline.account_input("codex", {"uuid": "unknown"},
                                   [[now - 300, now, 0, 0, 2, 0]], [], now),
        ])
        stale = timeline.provider_input(now - 86400, [])
        core.kv_set(con, "timeline_provider:antigravity", stale)
        for corruption in ('{bad JSON', '{"unexpected": true}',
                           json.dumps({**codex, "accounts": [{"meta": {"provider": "claude"},
                                                              "quota": [[0]], "spend": {}}]})):
            with self.subTest(corruption=corruption):
                con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", ("timeline_provider:claude", corruption))
                state = {"schema": 4}
                timeline.publish(con, state, now, {"codex": codex}, codex_only=True)
                overview = state["overviewTimeline"]
                self.assertEqual(overview["providers"], {
                    "claude": {"builtAt": None, "partial": True},
                    "codex": {"builtAt": now, "partial": False},
                    "antigravity": {"builtAt": stale["builtAt"], "partial": True},
                })
                combined = overview["ranges"]["24h"]["combined"]
                self.assertEqual(combined["totalYouUsd"], 7)
                self.assertEqual(combined["totalOthersUsd"], 5)
                self.assertEqual(combined["unratedAccounts"], [])
                self.assertEqual(combined["unknownAccounts"], ["codex:rated", "codex:unknown"])

    def test_publish_failure_exits_nonzero_and_keeps_previous_state(self):
        for args in (["--refresh"], ["--switch"], ["--switch", "--provider", "codex"]):
            with (self.subTest(args=args), tempfile.TemporaryDirectory() as directory,
                  patch.multiple(core, DATA=directory, DB=str(Path(directory) / "usage.db")),
                  patch.object(sys, "argv", ["tick.py", *args]), patch.object(tick, "history_import", None),
                  patch.object(tick, "antigravity", None), patch.object(tick.scan, "scan", return_value={}),
                  patch.object(tick.scan, "attribute", return_value=0), patch.object(tick.poll, "poll", return_value=[]),
                  patch.object(tick.codex, "import_quota", return_value={}),
                  patch.object(tick.codex, "block", return_value=({}, {})),
                  patch.object(tick.calib, "analyse", return_value={"generatedAt": 1795861532}),
                  patch.object(tick.os, "replace", side_effect=OSError("write failed"))):
                state = Path(directory) / "state.json"
                state.write_text("{}")
                with self.assertRaises(SystemExit) as result:
                    tick.main()
                self.assertEqual(result.exception.code, 2)
                self.assertIn("write failed", (Path(directory) / "tick.log").read_text())
                self.assertEqual(state.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
