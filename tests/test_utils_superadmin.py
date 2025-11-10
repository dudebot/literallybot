import os, shutil, unittest
from core.config import Config
from core.utils import get_superadmins, is_superadmin, is_admin

class DummyAuthor:
    def __init__(self, id):
        self.id = id
        class P: administrator=False
        self.guild_permissions = P()

class DummyGuild:
    def __init__(self, owner_id):
        self.id = 123
        self.owner = DummyAuthor(owner_id)

class DummyCtx:
    def __init__(self, user_id, guild=True):
        self.author = DummyAuthor(user_id)
        self.guild = DummyGuild(999) if guild else None
        class Bot: pass
        self.bot = Bot()
        self.bot.config = None

class TestUtilsNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = 'tmp_configs_test'
        os.makedirs(self.tmp, exist_ok=True)
        self.cfg = Config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_superadmins_scalar_normalizes(self):
        # write scalar
        self.cfg.set_global('superadmins', 42)
        sa = get_superadmins(self.cfg)
        self.assertEqual(sa, [42])
        # double-check idempotence
        sa2 = get_superadmins(self.cfg)
        self.assertEqual(sa2, [42])

    def test_is_superadmin_dual_signatures(self):
        self.cfg.set_global('superadmins', [1001])
        # classic signature
        self.assertTrue(is_superadmin(self.cfg, 1001))
        # ctx signature
        ctx = DummyCtx(1001)
        ctx.bot.config = self.cfg
        self.assertTrue(is_superadmin(ctx))
        self.assertFalse(is_superadmin(self.cfg, 2002))

    def test_is_admin_respects_guild_admins(self):
        ctx = DummyCtx(3003)
        ctx.bot.config = self.cfg
        # grant as guild admin via config
        admins = self.cfg.get(ctx, 'admins', [])
        admins.append(3003)
        self.cfg.set(ctx, 'admins', admins)
        self.assertTrue(is_admin(self.cfg, ctx))
        self.assertTrue(is_admin(ctx))

if __name__ == '__main__':
    unittest.main()
