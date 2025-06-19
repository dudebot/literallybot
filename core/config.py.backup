import os, json
import time
from threading import Timer, Lock

class Config:
    def __init__(self, config_dir='configs'):
        self.config_dir = config_dir
        os.makedirs(self.config_dir, exist_ok=True)
        self._configs = {}  # maps config_id (str) to config dict
        self._dirty_configs = set()  # Track what needs saving
        self._save_timer = None
        self._save_delay = 5.0  # seconds
        self._lock = Lock()  # Thread safety for timer operations
        self._load_all()

    def _load_all(self):
        for fname in os.listdir(self.config_dir):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(self.config_dir, fname)
            with open(path, 'r') as f:
                data = json.load(f)
            
            config_id = fname[:-5]  # Remove .json extension
            self._configs[config_id] = data
        
        # ensure global config exists
        if 'global' not in self._configs:
            self._configs['global'] = {}
            self._immediate_save('global')

    def _resolve_config_id(self, ctx, scope='guild'):
        """Resolve context to config file identifier"""
        if scope == 'global' or ctx is None:
            return 'global'
        elif scope == 'user':
            if hasattr(ctx, 'author'):
                return f'user_{ctx.author.id}'
            elif isinstance(ctx, int):
                return f'user_{ctx}'
            else:
                raise ValueError("Cannot resolve user context")
        elif scope == 'guild':
            if hasattr(ctx, 'guild') and getattr(ctx.guild, 'id', None) is not None:
                return str(ctx.guild.id)
            elif isinstance(ctx, int):
                return str(ctx)
            else:
                return 'global'  # fallback for DMs
        else:
            raise ValueError(f"Invalid scope: {scope}")

    def _immediate_save(self, config_id):
        """Save immediately without buffering"""
        fname = f'{config_id}.json'
        path = os.path.join(self.config_dir, fname)
        temp_path = path + '.tmp'
        
        try:
            # Atomic write
            with open(temp_path, 'w') as f:
                json.dump(self._configs.get(config_id, {}), f, indent=4)
            
            # Cross-platform atomic rename
            if os.name == 'nt':  # Windows
                # On Windows, need to remove target first
                if os.path.exists(path):
                    os.remove(path)
                os.rename(temp_path, path)
            else:  # Unix/Linux - supports atomic replace
                os.rename(temp_path, path)
                
        except Exception as e:
            # Clean up temp file if something goes wrong
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
            raise e

    def _schedule_save(self):
        """Schedule a delayed save to batch writes"""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = Timer(self._save_delay, self._timer_flush)
            self._save_timer.daemon = True  # Allow program to exit
            self._save_timer.start()

    def _timer_flush(self):
        """Timer callback that acquires lock before flushing"""
        with self._lock:
            self._flush_all()

    def _flush_all(self):
        """Write all dirty configs to disk - assumes lock is already held"""
        for config_id in list(self._dirty_configs):  # Copy to avoid modification during iteration
            self._immediate_save(config_id)
        self._dirty_configs.clear()
        self._save_timer = None

    def flush(self):
        """Manually flush all pending writes"""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
            self._flush_all()

    def get(self, ctx, key, default=None, scope='guild'):
        """Get a config value from guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        cfg = self._configs.setdefault(config_id, {})
        
        if key not in cfg and default is not None:
            cfg[key] = default
            self._dirty_configs.add(config_id)
            self._schedule_save()
        
        return cfg.get(key, default)

    def set(self, ctx, key, value, scope='guild'):
        """Set a config value in guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        cfg = self._configs.setdefault(config_id, {})
        cfg[key] = value
        self._dirty_configs.add(config_id)
        self._schedule_save()

    # Convenience methods for user configs
    def get_user(self, ctx, key, default=None):
        """Get a user-specific config value"""
        return self.get(ctx, key, default, scope='user')

    def set_user(self, ctx, key, value):
        """Set a user-specific config value"""
        self.set(ctx, key, value, scope='user')

    # Convenience methods for global configs
    def get_global(self, key, default=None):
        """Get a global config value"""
        return self.get(None, key, default, scope='global')

    def set_global(self, key, value):
        """Set a global config value"""
        self.set(None, key, value, scope='global')

    def shutdown(self):
        """Clean shutdown - flush all pending writes and cancel timers"""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
            self._flush_all()
