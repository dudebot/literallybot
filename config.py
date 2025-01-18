import json
import os

class Config:
    def __init__(self, server_id):
        self.server_id = server_id
        self.config = {
            "cogs": [],
            "bot_operators": [],
            "dynamic_responses": {},
            "whitelist_roles": []
        }
        self.load_config()

    def load_config(self):
        config_path = f"configs/{self.server_id}.json"
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                self.config = json.load(f)
        else:
            self.save_config()

    def save_config(self):
        config_path = f"configs/{self.server_id}.json"
        with open(config_path, "w") as f:
            json.dump(self.config, f, indent=4)

    def enable_cog(self, cog_name):
        if cog_name not in self.config["cogs"]:
            self.config["cogs"].append(cog_name)
            self.save_config()

    def disable_cog(self, cog_name):
        if cog_name in self.config["cogs"]:
            self.config["cogs"].remove(cog_name)
            self.save_config()

    def add_bot_operator(self, user_id):
        if user_id not in self.config["bot_operators"]:
            self.config["bot_operators"].append(user_id)
            self.save_config()

    def remove_bot_operator(self, user_id):
        if user_id in self.config["bot_operators"]:
            self.config["bot_operators"].remove(user_id)
            self.save_config()

    def set_dynamic_responses(self, responses):
        self.config["dynamic_responses"] = responses
        self.save_config()

    def add_whitelist_role(self, role_name):
        if role_name not in self.config["whitelist_roles"]:
            self.config["whitelist_roles"].append(role_name)
            self.save_config()

    def remove_whitelist_role(self, role_name):
        if role_name in self.config["whitelist_roles"]:
            self.config["whitelist_roles"].remove(role_name)
            self.save_config()
