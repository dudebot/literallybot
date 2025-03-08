import json, os

class Config:
    def __init__(self, ctx=None):
        directory = os.path.join("configs")
        if isinstance(ctx, int):
            self.config_path = os.path.join(directory, f"{ctx}.json")
        elif ctx and hasattr(ctx, 'guild') and hasattr(ctx.guild, 'id'):
            self.config_path = os.path.join(directory, f"{ctx.guild.id}.json")
        else:
            self.config_path = os.path.join(directory, "global.json")
        if not os.path.exists(directory):
            os.makedirs(directory)
        if not os.path.exists(self.config_path):
            if ctx and hasattr(ctx, 'guild') and getattr(ctx.guild, 'name', None):
                self.config = {"guild_name": ctx.guild.name}  # default with guild name included
            else:
                self.config = {}
            self.save_config()
        else:
            self.load_config()

    def load_config(self):
        with open(self.config_path, "r") as f:
            self.config = json.load(f)

    def save_config(self):
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=4)

    def get(self, key, default=None):
        if key not in self.config:
            self.config[key] = default
            self.save_config()
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save_config()

    def enable_cog(self, cog_name):
        if "cogs" not in self.config:
            self.config["cogs"] = []
        if cog_name not in self.config["cogs"]:
            self.config["cogs"].append(cog_name)
            self.save_config()

    def disable_cog(self, cog_name):
        if "cogs" in self.config and cog_name in self.config["cogs"]:
            self.config["cogs"].remove(cog_name)
            self.save_config()

    def add_bot_operator(self, operator_id):
        if "bot_operators" not in self.config:
            self.config["bot_operators"] = []
        if operator_id not in self.config["bot_operators"]:
            self.config["bot_operators"].append(operator_id)
            self.save_config()

    def remove_bot_operator(self, operator_id):
        if "bot_operators" in self.config and operator_id in self.config["bot_operators"]:
            self.config["bot_operators"].remove(operator_id)
            self.save_config()

    def add_whitelist_role(self, role):
        if "whitelist_roles" not in self.config:
            self.config["whitelist_roles"] = []
        if role not in self.config["whitelist_roles"]:
            self.config["whitelist_roles"].append(role)
            self.save_config()

    def remove_whitelist_role(self, role):
        if "whitelist_roles" in self.config and role in self.config["whitelist_roles"]:
            self.config["whitelist_roles"].remove(role)
            self.save_config()

    def set_dynamic_responses(self, responses):
        self.config["dynamic_responses"] = responses
        self.save_config()
