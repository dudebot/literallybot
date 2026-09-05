import os, json
import time
from threading import Timer, Lock

# Config files hold plaintext secrets (the Discord token since #83, provider
# API keys, the MCP bearer token), so the store is owner-only on disk: the
# directory 0700, every file 0600. Best-effort — on Windows os.chmod only
# moves the read-only bit and the calls are harmless no-ops there.
DIR_MODE = 0o700
FILE_MODE = 0o600


def _harden(path, mode):
    """chmod that never breaks a working bot over a permissions nicety.

    A config store on a filesystem that can't represent POSIX modes (a Windows
    share, some container volume mounts) is a downgrade in hardening, not a
    reason to refuse to start.
    """
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class Config:
    def __init__(self, config_dir='configs'):
        self.config_dir = config_dir
        os.makedirs(self.config_dir, mode=DIR_MODE, exist_ok=True)
        # makedirs' mode is ignored when the directory already exists (and is
        # masked by umask when it doesn't), so set it explicitly either way.
        _harden(self.config_dir, DIR_MODE)
        self._configs = {}  # maps config_id (str) to config dict
        self._dirty_configs = set()  # Track what needs saving
        self._file_mtimes = {}  # Track file modification times
        self._save_timer = None
        self._reload_timer = None
        self._save_delay = 5.0  # seconds
        self._reload_delay = 2.0  # seconds - check for external changes
        self._lock = Lock()  # Thread safety for timer operations
        self._data_lock = Lock()  # Thread safety for config dict access
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
            # Atomic write, tmp+rename. These files carry plaintext secrets,
            # so the temp file is opened 0600 up front.
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
            with os.fdopen(fd, 'w') as f:
                # Tighten BEFORE writing any content. O_CREAT's mode is masked
                # by umask, and is ignored outright when the temp file already
                # exists (a leftover from a previous crash keeps its old, maybe
                # world-readable, mode) — so a chmod after json.dump would
                # leave the secret briefly readable. fchmod targets the open
                # descriptor, so it can't be redirected by a swapped path.
                try:
                    os.fchmod(f.fileno(), FILE_MODE)
                except (OSError, AttributeError):
                    pass  # best-effort: Windows has no fchmod
                json.dump(self._configs.get(config_id, {}), f, indent=4)

            # Cross-platform atomic rename
            if os.name == 'nt':  # Windows
                # On Windows, need to remove target first
                if os.path.exists(path):
                    os.remove(path)
                os.rename(temp_path, path)
            else:  # Unix/Linux - supports atomic replace
                os.rename(temp_path, path)
            _harden(path, FILE_MODE)

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

    def guild_ids(self):
        """All guild ids with a loaded config (guild configs are the
        digit-named files). The public enumeration API — callers must use
        this rather than reaching into the private storage dicts."""
        return [int(cid) for cid in self._configs if cid.isdigit()]

    def global_keys(self):
        """All keys currently present in the global config — the public
        key-enumeration API (same contract as guild_ids: never scan the
        private storage dicts)."""
        config_id = self._resolve_config_id(None, 'global')
        with self._data_lock:
            return list(self._configs.get(config_id, {}).keys())

    def get(self, ctx, key, default=None, scope='guild'):
        """Get a config value from guild, user, or global scope. Read-only - does not persist defaults."""
        config_id = self._resolve_config_id(ctx, scope)
        with self._data_lock:
            cfg = self._configs.get(config_id, {})
            return cfg.get(key, default)

    def set(self, ctx, key, value, scope='guild'):
        """Set a config value in guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        with self._data_lock:
            cfg = self._configs.setdefault(config_id, {})
            cfg[key] = value
        self._dirty_configs.add(config_id)
        self._schedule_save()

    def rem(self, ctx, key, scope='guild'):
        """Remove a config key from guild, user, or global scope"""
        config_id = self._resolve_config_id(ctx, scope)
        with self._data_lock:
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
        """Take the on-disk file wholesale; file wins over in-memory state."""
        with self._data_lock:
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
