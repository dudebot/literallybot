"""Location of each guild's media library: media/<guild_id>/.

The media cog owns the library's contents; other features that read from it
locate it here instead of importing that cog.
"""
from pathlib import Path

MEDIA_ROOT = Path('media')


def safe_media_directory(directory) -> Path:
    """Absolute path of a directory inside the media root. Raises ValueError
    if it is outside the root or any part of it, the root included, is a
    symlink."""
    root = MEDIA_ROOT.absolute()
    directory = Path(directory).absolute()
    try:
        relative = directory.relative_to(root)
    except ValueError:
        raise ValueError("Media directory is outside the library.") from None
    current = root
    if current.is_symlink():
        raise ValueError("Media directories cannot be symbolic links.")
    for part in relative.parts:
        if part == '..':
            raise ValueError("Media directory is outside the library.")
        current = current / part
        if current.is_symlink():
            raise ValueError("Media directories cannot be symbolic links.")
    return directory


def guild_media_dir(guild_id) -> Path:
    """Absolute path of the guild's library. Raises ValueError if unsafe."""
    return safe_media_directory(MEDIA_ROOT / str(int(guild_id)))


def guild_media_files(guild_id) -> list[Path]:
    """Regular, non-hidden, non-symlink files in the guild's library, sorted
    by name; [] when the library is absent (a guild that never added media
    has an empty library, not an error)."""
    try:
        entries = list(guild_media_dir(guild_id).iterdir())
    except (OSError, ValueError):
        return []
    return sorted((p for p in entries
                   if not p.name.startswith('.') and not p.is_symlink() and p.is_file()),
                  key=lambda p: p.name.lower())
