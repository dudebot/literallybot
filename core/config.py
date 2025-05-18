import os, json

class Config:
    def __init__(self, config_dir='configs'):
        self.config_dir = config_dir
        os.makedirs(self.config_dir, exist_ok=True)
        self._configs = {}  # maps guild_id (int) or None for global to config dict
        self._load_all()

    def _load_all(self):
        for fname in os.listdir(self.config_dir):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(self.config_dir, fname)
            with open(path, 'r') as f:
                data = json.load(f)
            key = fname[:-5]
            if key == 'global':
                gid = None
            else:
                try:
                    gid = int(key)
                except ValueError:
                    continue
            self._configs[gid] = data
        # ensure global config exists
        if None not in self._configs:
            self._configs[None] = {}
            self._save(None)

    def _save(self, gid):
        fname = 'global.json' if gid is None else f'{gid}.json'
        path = os.path.join(self.config_dir, fname)
        with open(path, 'w') as f:
            json.dump(self._configs.get(gid, {}), f, indent=4)

    def get(self, ctx, key, default=None):
        # Determine guild key
        if ctx is None:
            gid = None # global config
        elif hasattr(ctx, 'guild') and getattr(ctx.guild, 'id', None) is not None:
            gid = ctx.guild.id
        elif isinstance(ctx, int):
            gid = ctx
        else:
            gid = None # global config
        cfg = self._configs.setdefault(gid, {})
        if key not in cfg:
            cfg[key] = default
            self._save(gid)
        return cfg.get(key)

    def set(self, ctx, key, value):
        # Determine guild key
        if hasattr(ctx, 'guild') and getattr(ctx.guild, 'id', None) is not None:
            gid = ctx.guild.id
        elif isinstance(ctx, int):
            gid = ctx
        else:
            gid = None
        cfg = self._configs.setdefault(gid, {})
        cfg[key] = value
        self._save(gid)
