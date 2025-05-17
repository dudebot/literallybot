import sys
import unittest
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.server_id = 123456789
        self.config = Config(self.server_id)
        self.config_path = f"configs/{self.server_id}.json"
        if not os.path.exists("configs"):
            os.makedirs("configs")

    def tearDown(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    # def test_load_config(self):
    #     self.config.config["cogs"] = ["test_cog"]
    #     self.config.save_config()
    #     new_config = Config(self.server_id)
    #     self.assertEqual(new_config.config["cogs"], ["test_cog"])

    # def test_save_config(self):
    #     self.config.config["cogs"] = ["test_cog"]
    #     self.config.save_config()
    #     with open(self.config_path, "r") as f:
    #         data = json.load(f)
    #     self.assertEqual(data["cogs"], ["test_cog"])

    # def test_enable_cog(self):
    #     self.config.enable_cog("test_cog")
    #     self.assertIn("test_cog", self.config.config["cogs"])

    # def test_disable_cog(self):
    #     self.config.enable_cog("test_cog")
    #     self.config.disable_cog("test_cog")
    #     self.assertNotIn("test_cog", self.config.config["cogs"])

    # def test_add_bot_operator(self):
    #     self.config.add_bot_operator(987654321)
    #     self.assertIn(987654321, self.config.config["bot_operators"])

    # def test_remove_bot_operator(self):
    #     self.config.add_bot_operator(987654321)
    #     self.config.remove_bot_operator(987654321)
    #     self.assertNotIn(987654321, self.config.config["bot_operators"])

    # def test_set_dynamic_responses(self):
    #     responses = {"key": ["value1", "value2"]}
    #     self.config.set_dynamic_responses(responses)
    #     self.assertEqual(self.config.config["dynamic_responses"], responses)

    # def test_add_whitelist_role(self):
    #     self.config.add_whitelist_role("test_role")
    #     self.assertIn("test_role", self.config.config["whitelist_roles"])

    # def test_remove_whitelist_role(self):
    #     self.config.add_whitelist_role("test_role")
    #     self.config.remove_whitelist_role("test_role")
    #     self.assertNotIn("test_role", self.config.config["whitelist_roles"])

if __name__ == '__main__':
    unittest.main()
