import os
import json
import time
import shutil
import tempfile
import unittest

from core.config import Config

class TestConfigLocal(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cfg-")
        self.cfg = Config(config_dir=self.tmpdir)

    def tearDown(self):
        try:
            self.cfg.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_global_set_get_and_persist(self):
        self.cfg.set_global("foo", "bar")
        # not flushed yet; in-memory visible
        self.assertEqual(self.cfg.get_global("foo"), "bar")
        # force flush and verify file
        self.cfg.flush()
        with open(os.path.join(self.tmpdir, "global.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("foo"), "bar")

    def test_guild_scope(self):
        guild_id = 123
        self.cfg.set(guild_id, "x", 1, scope="guild")
        self.assertEqual(self.cfg.get(guild_id, "x", scope="guild"), 1)
        self.cfg.flush()
        path = os.path.join(self.tmpdir, f"{guild_id}.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("x"), 1)

    def test_user_scope(self):
        class Dummy:
            def __init__(self, id):
                self.id = id
        u = Dummy(555)
        self.cfg.set_user(u, "theme", "dark")
        self.assertEqual(self.cfg.get_user(u, "theme"), "dark")
        self.cfg.flush()
        path = os.path.join(self.tmpdir, "user_555.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("theme"), "dark")

    def test_external_reload_wins(self):
        # write a value and flush
        self.cfg.set_global("k", 1)
        self.cfg.flush()
        gpath = os.path.join(self.tmpdir, "global.json")
        # external change
        with open(gpath, "w", encoding="utf-8") as f:
            json.dump({"k": 2}, f)
        # bump mtime
        os.utime(gpath, None)
        # wait briefly for reload timer to tick
        time.sleep(3)
        self.assertEqual(self.cfg.get_global("k"), 2)

if __name__ == "__main__":
    unittest.main()
