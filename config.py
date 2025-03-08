import json, os

class Config:
    def __init__(self, ctx=None):
        directory = os.path.join("configs")
        if ctx:
            self.config_path = os.path.join(directory, f"{ctx.guild.id}.json")
        else:
            self.config_path = os.path.join(directory, "global.json")
        if not os.path.exists(directory):
            os.makedirs(directory)
        if not os.path.exists(self.config_path):
            if ctx and ctx.guild and ctx.guild.name:
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
