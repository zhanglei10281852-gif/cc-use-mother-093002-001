"""规范化 JSON 与摘要工具，保证事件与证据可核验。"""
import hashlib
import json

GENESIS_HASH = "0" * 64


def canonical_json(obj) -> str:
    """生成键序稳定、无空白的 JSON 文本，用于哈希。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
