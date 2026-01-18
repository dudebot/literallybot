import os, json
import time
from threading import Timer, Lock

class Config:
    def __init__(self, config_dir='configs'):
        self.config_dir = config_dir
        os.makedirs(self.config_dir, exist_ok=True)
        self._configs = {}  # maps config_id (str) to config dict
        self._dirty_configs = set()  # Track what needs saving
        self._file_mtimes = {}  # Track file modification times
        self._save_timer = None
        self._reload_timer = None
        self._save_delay = 5.0  # seconds
        self._reload_delay = 2.0  # seconds - check for external changes
        self._lock = Lock()  # Thread safety for timer operations
        self._writing = False  # Flag to prevent read-during-write
        self._load_all()
        self._schedule_reload()  # Start monitoring for external changes

    def _load_all(self):
        for fname in os.listdir(self.config_dir):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(self.config_dir, fname)
            with open(path, 'r') as f:
                data = json.load(f)
            
            config_id = fname[:-5]  # Remove .json extension
            self._configs[config_id] = data
            # Track initial modification time
            self._file_mtimes[config_id] = os.path.getmtime(path)
        
        # ensure global config exists
        if 'global' not in self._configs:
            self._configs['global'] = {}
            self._immediate_save('global')

    def _resolve_config_id(self, ctx, scope='guild'):
        """Resolve context to config file identifier"""
        if scope == 'global' or ctx is None:
            return 'global'
        elif scope == 'user':
            if hasattr(ctx, 'id'):
                return f'user_{ctx.id}'
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
        
        self._writing = True  # Set flag to prevent reload during write
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
            
            # Update modification time after successful write
            self._file_mtimes[config_id] = os.path.getmtime(path)
                
        except Exception as e:
            # Clean up temp file if something goes wrong
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
            raise e
        finally:
            self._writing = False  # Clear flag

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
        """Get a config value from guild, user, or global scope. Read-only - does not persist defaults."""
        config_id = self._resolve_config_id(ctx, scope)
        cfg = self._configs.setdefault(config_id, {})
        return cfg.get(key, default)

    def set(self, ctx, key, value, scope='guild'):
        """Set a config value in guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        cfg = self._configs.setdefault(config_id, {})
        cfg[key] = value
        self._dirty_configs.add(config_id)
        self._schedule_save()

    def rem(self, ctx, key, scope='guild'):
        """Remove a config key from guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        if config_id in self._configs and key in self._configs[config_id]:
            del self._configs[config_id][key]
            self._dirty_configs.add(config_id)
            self._schedule_save()
            return True
        return False

    def has(self, ctx, key, scope='guild'):
        """Check if a config key exists in guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        return config_id in self._configs and key in self._configs[config_id]

    # Convenience methods for user configs
    def get_user(self, ctx, key, default=None):
        """Get a user-specific config value"""
        return self.get(ctx, key, default, scope='user')

    def set_user(self, ctx, key, value):
        """Set a user-specific config value"""
        self.set(ctx, key, value, scope='user')

    def rem_user(self, ctx, key):
        """Remove a user-specific config value"""
        return self.rem(ctx, key, scope='user')

    def has_user(self, ctx, key):
        """Check if a user-specific config key exists"""
        return self.has(ctx, key, scope='user')

    # Convenience methods for global configs
    def get_global(self, key, default=None):
        """Get a global config value"""
        return self.get(None, key, default, scope='global')

    def set_global(self, key, value):
        """Set a global config value"""
        self.set(None, key, value, scope='global')

    def rem_global(self, key):
        """Remove a global config value"""
        return self.rem(None, key, scope='global')

    def has_global(self, key):
        """Check if a global config key exists"""
        return self.has(None, key, scope='global')

    def _schedule_reload(self):
        """Schedule periodic check for external file changes"""
        with self._lock:
            if self._reload_timer:
                self._reload_timer.cancel()
            self._reload_timer = Timer(self._reload_delay, self._timer_reload)
            self._reload_timer.daemon = True
            self._reload_timer.start()
    
    def _timer_reload(self):
        """Timer callback to check for external changes"""
        with self._lock:
            self._check_external_changes()
        self._schedule_reload()  # Reschedule next check outside lock
    
    def _merge_configs(self, config_id, external_data):
        """Merge external changes with current config, handling conflicts"""
        current_data = self._configs.get(config_id, {})
        
        # If this config is dirty, we need to handle conflicts
        if config_id in self._dirty_configs:
            conflicts = []
            
            # Find keys that exist in both and have different values
            for key in external_data:
                if key in current_data and current_data[key] != external_data[key]:
                    conflicts.append({
                        'key': key,
                        'memory_value': current_data[key],
                        'file_value': external_data[key]
                    })
            
            if conflicts:
                print(f"[Config] Merge conflicts detected in {config_id}.json:")
                for conflict in conflicts:
                    print(f"  - Key '{conflict['key']}': memory={conflict['memory_value']}, file={conflict['file_value']} (using file value)")
        
        # Merge: external data takes precedence
        self._configs[config_id] = external_data.copy()
    
    def _check_external_changes(self):
        """Check for external file modifications and reload if needed"""
        if self._writing:
            return  # Skip if we're currently writing
        
        for fname in os.listdir(self.config_dir):
            if not fname.endswith('.json'):
                continue
            
            config_id = fname[:-5]
            path = os.path.join(self.config_dir, fname)
            
            try:
                current_mtime = os.path.getmtime(path)
                
                # Check if this is a new file or was modified externally
                is_new = config_id not in self._file_mtimes
                is_modified = not is_new and current_mtime > self._file_mtimes[config_id]
                
                if is_new or is_modified:
                    # Load the external changes
                    with open(path, 'r') as f:
                        external_data = json.load(f)
                    
                    # Merge with current config
                    self._merge_configs(config_id, external_data)
                    
                    # Update modification time
                    self._file_mtimes[config_id] = current_mtime
                    
                    # If this config was dirty, it's not anymore (external changes win)
                    if config_id in self._dirty_configs:
                        self._dirty_configs.remove(config_id)
                    
                    action = "Loaded new" if is_new else "Reloaded"
                    print(f"[Config] {action} {config_id}.json due to external changes")
                    
            except (OSError, json.JSONDecodeError) as e:
                print(f"[Config] Error reloading {config_id}.json: {e}")
    
    def shutdown(self):
        """Clean shutdown - flush all pending writes and cancel timers"""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
            if self._reload_timer:
                self._reload_timer.cancel()
                self._reload_timer = None
            self._flush_all()
