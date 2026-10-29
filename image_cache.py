"""短时保存入站原图，供尚未进入聊天上下文的课表指令读取；不写磁盘。"""

from time import monotonic


class InboundImages:
    TTL = 600
    MAX_ITEMS = 16
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self):
        self.entries = {}

    @staticmethod
    def key(message):
        info = message.get("message_info") or {}
        group = str((info.get("group_info") or {}).get("group_id") or "")
        user = str((info.get("user_info") or {}).get("user_id") or "")
        mid = str(message.get("message_id") or "")
        if not all((message.get("platform"), group, user, mid)):
            return None
        return (message["platform"], str(message.get("account_id") or ""),
                str(message.get("scope") or ""), group, user, mid)

    def prune(self):
        now = monotonic()
        self.entries = {k: v for k, v in self.entries.items() if v[0] > now}

    def remember(self, message):
        self.prune()
        key = self.key(message)
        images = [x for x in message.get("raw_message", []) if isinstance(x, dict) and x.get("type") == "image"]
        if key is None or not 1 <= len(images) <= 4:
            return
        if any(not isinstance(x.get("binary_data_base64"), str) or not x["binary_data_base64"] for x in images):
            return
        size = sum(len(x["binary_data_base64"]) for x in images)
        if size > self.MAX_BYTES:
            return
        self.entries.pop(key, None)
        while self.entries and (len(self.entries) >= self.MAX_ITEMS
                or sum(v[2] for v in self.entries.values()) + size > self.MAX_BYTES):
            self.entries.pop(next(iter(self.entries)))
        self.entries[key] = (monotonic() + self.TTL, [
            {"hash": x.get("hash"), "binary_data_base64": x["binary_data_base64"]} for x in images], size)

    def restore(self, message):
        self.prune()
        saved = self.entries.get(self.key(message))
        if saved is None:
            return message
        images = [x for x in message.get("raw_message", []) if isinstance(x, dict) and x.get("type") == "image"]
        if len(images) != len(saved[1]) or any(x.get("hash") and x["hash"] != y.get("hash")
                                               for x, y in zip(images, saved[1], strict=True)):
            return message
        iterator = iter(saved[1])
        segments = []
        for segment in message["raw_message"]:
            if isinstance(segment, dict) and segment.get("type") == "image":
                cached = next(iterator)
                segment = {**segment, "binary_data_base64": segment.get("binary_data_base64") or cached["binary_data_base64"]}
            segments.append(segment)
        return {**message, "raw_message": segments}

    def clear(self):
        self.entries.clear()
