"""Discord message deletion, outbound text safety, and bounded splitting."""
import re
import unicodedata

import discord


async def safe_delete(ctx, logger=None):
    """Safely attempt to delete a command message without raising exceptions.

    Args:
        ctx: Discord command context
        logger: Optional logger instance for warnings

    Returns:
        bool: True if deletion succeeded, False otherwise
    """
    try:
        await ctx.message.delete()
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        if logger:
            channel_name = getattr(ctx.channel, "name", ctx.channel.id)
            logger.warning(f"Unable to delete command message in {channel_name}: {exc}")
        return False


# Cf we keep because they are part of emoji sequences, not hidden payloads.
_KEEP_FORMAT = frozenset("\u200d\ufe0e\ufe0f")

# Discord markdown only treats ASCII ` ' " as markup. NFKC folds fullwidth
# grave (U+FF40) to ` but leaves curly quotes alone; these leftovers are
# the lookalikes models actually emit.
_MARKDOWN_PUNCT = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u2035": "'", "\u02bc": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2033": '"', "\u2036": '"',
    "\u02cb": "`", "\u02f4": "`", "\u1fef": "`",
})


def sanitize_outbound_text(text: str) -> str:
    """Fold lookalike punctuation and strip hidden Unicode from model text.

    Applied on every model-originated Discord send so inline code actually
    renders, and so zero-width / bidi / tag characters cannot hide a
    payload. Keeps emoji ZWJ sequences and variation selectors. Does not
    rewrite visible non-Latin letters.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_MARKDOWN_PUNCT)
    out = []
    for ch in text:
        if ch in _KEEP_FORMAT or ch in "\n\t":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in {"Cf", "Cc"}:
            continue
        if cat == "Zs":
            out.append(" ")
            continue
        if cat in {"Zl", "Zp"}:
            out.append("\n")
            continue
        out.append(ch)
    return "".join(out)


def recursive_split(text, max_size=2000):
    """Split text into Discord-size chunks, preferring newline/sentence/space
    boundaries and keeping fenced/inline code blocks intact across chunks.
    The single shared implementation of Discord's 2000-char message limit."""
    if len(text) <= max_size:
        return [text]
    mid = len(text) // 2

    # Updated pattern with optional language capture for code blocks.
    code_block_pattern = r'(`{3,})(\w+)?\n[\s\S]*?\n\1'
    inline_code_pattern = r'(`+)[^`]+?\1'

    code_blocks = list(re.finditer(code_block_pattern, text))
    inline_codes = list(re.finditer(inline_code_pattern, text))

    for pattern in [r'\n+', r'\.\s+', r'\s+']:
        matches = list(re.finditer(pattern, text))
        if matches:
            best_match = min(matches, key=lambda m: abs(m.start() - mid))
            split_index = best_match.end()
            if split_index <= 0 or split_index >= len(text):
                continue

            inside_code_block = False
            code_delimiter = None
            code_lang = ""
            for block in code_blocks:
                if block.start() < split_index < block.end():
                    inside_code_block = True
                    code_delimiter = block.group(1)  # e.g. "```"
                    code_lang = block.group(2) if block.group(2) else ""
                    break

            inside_inline_code = any(code.start() < split_index < code.end() for code in inline_codes)

            left = text[:split_index].rstrip()
            right = text[split_index:].lstrip()

            if inside_code_block and code_delimiter:
                header = code_delimiter + code_lang  # Preserve language specifier.
                if not left.endswith(header):
                    left = left + "\n" + "```"
                if not right.startswith(header):
                    right = header + "\n" + right

            if inside_inline_code:
                if not left.endswith("`"):
                    left = left.rstrip("`") + "`"
                if not right.startswith("`"):
                    right = "`" + right.lstrip("`")

            # Progress guard: fence repair can grow a side back to (or past)
            # the original length when a single fenced block exceeds max_size,
            # which would recurse forever — fall through to the hard cut,
            # which always shrinks the input.
            if len(left) >= len(text) or len(right) >= len(text):
                continue

            return recursive_split(left, max_size) + recursive_split(right, max_size)

    split_index = max_size
    inside_code_block = False
    code_delimiter = None
    code_lang = ""
    for block in code_blocks:
        if block.start() < max_size < block.end():
            inside_code_block = True
            code_delimiter = block.group(1)
            code_lang = block.group(2) if block.group(2) else ""
            break
    inside_inline_code = any(code.start() < max_size < code.end() for code in inline_codes)
    if inside_code_block and code_delimiter:
        header = code_delimiter + code_lang
        closing = "\n" + code_delimiter
        split_index -= len(closing)
        # Very long delimiters/language labels can leave no room for content.
        # Fall back to a literal bounded cut rather than recurse without progress.
        if split_index > len(header) + 1:
            left = text[:split_index].rstrip() + closing
            right = header + "\n" + text[split_index:].lstrip()
            return [left] + recursive_split(right, max_size)
    elif inside_inline_code:
        delimiter = next(code.group(1) for code in inline_codes
                         if code.start() < max_size < code.end())
        split_index -= len(delimiter)
        if split_index > len(delimiter):
            left = text[:split_index].rstrip() + delimiter
            right = delimiter + text[split_index:].lstrip()
            return [left] + recursive_split(right, max_size)
    left = text[:max_size].rstrip()
    right = text[max_size:].lstrip()
    return [left] + recursive_split(right, max_size)
