"""课程名匹配：优先文字与常用简称，找不到时才尝试受限缩写和单字误写。"""

from typing import Iterable

import re
import unicodedata

# 同组名称归一到第一个全称。这里只维护课程术语，不从聊天记忆扩充别名。
SUBJECT_NAMES = (
    ("高等数学", "高数"), ("大学英语", "大英"), ("大学物理", "大物"),
    ("线性代数", "线代"), ("概率论与数理统计", "概统", "概率统计", "概率与数理统计"),
    ("计算机网络", "计网"), ("计算机组成原理", "计组"), ("操作系统", "os"),
    ("马克思主义基本原理", "马原"), ("思想道德与法治", "思修"),
    ("中国近现代史纲要", "史纲"),
    ("毛泽东思想和中国特色社会主义理论体系概论", "毛概"),
    ("习近平新时代中国特色社会主义思想概论", "习概"),
)


def normalized(value: str) -> str:
    # +/# 区分 C、C++、C#，不能和普通标点一起删除。
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold()
                   if char.isalnum() or char in "+#")


ALIASES = sorted(((normalized(name), normalized(group[0])) for group in SUBJECT_NAMES for name in group),
                 key=lambda pair: len(pair[0]), reverse=True)


def canonical(value: str) -> str:
    for name, target in ALIASES:
        if value.startswith(name):
            return target + value[len(name):]
    return value


def abbreviation(query: str, title: str) -> bool:
    if not re.fullmatch(r"[\u3400-\u9fff]{2,}", query) or not title.startswith(query[0]):
        return False
    minimum_ratio = 0.5 if len(query) == 2 else 0.4
    if len(query) / max(len(title), 1) < minimum_ratio:
        return False
    chars = iter(title)
    return all(any(char == wanted for char in chars) for wanted in query)


def one_edit_apart(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    i = j = mistakes = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        mistakes += 1
        if mistakes > 1:
            return False
        if len(left) == len(right):
            i += 1
        j += 1
    return mistakes + len(left) - i + len(right) - j <= 1


def typo(query: str, title: str) -> bool:
    # 仅为中文课名移除常见分册尾缀；不改变 C++、英文单词的含义。
    core = re.sub(r"[a-ci0-9上下]+$", "", title) if re.search(r"[\u3400-\u9fff]", title) else title
    return one_edit_apart(query, title) or one_edit_apart(query, core)


def match_subjects(subject: str, titles: Iterable[str]) -> list[dict[str, str]]:
    if not isinstance(subject, str) or len(subject) > 100:
        raise ValueError("科目关键词不能超过 100 个字符")
    query = normalized(subject)
    if not query or not any(char.isalnum() for char in query):
        if subject.strip():
            raise ValueError("科目关键词应包含文字或数字，不能只有标点")
        return []
    wanted = canonical(query)
    short_alias = wanted != query and len(query) < 4
    strong, weak = [], []
    for title in sorted(set(titles)):
        text = normalized(title)
        expanded = canonical(text)
        direct = query in text
        if query == "os":
            direct = bool(re.search(r"(?<![a-z])os(?![a-z])", unicodedata.normalize("NFKC", title).casefold()))
        if direct:
            kind = "exact" if query == text else "substring"
            strong.append({"subject": title, "match_type": kind})
        elif wanted in expanded:
            strong.append({"subject": title, "match_type": "alias"})
        elif not short_alias and abbreviation(query, text):
            weak.append({"subject": title, "match_type": "abbreviation"})
        elif not short_alias and typo(wanted, expanded):
            weak.append({"subject": title, "match_type": "typo"})
    # 已存在可靠匹配时不混入近似名字，例如大学英语不能顺带命中大学物理。
    return strong if strong else weak


def subject_matches(subject: str, titles: Iterable[str]) -> tuple[set[str], dict]:
    matches = match_subjects(subject, titles)
    return {row["subject"] for row in matches}, {
        "query": subject.strip(), "matched_subjects": matches[:100], "total_subjects": len(matches),
        "ambiguous": len(matches) > 1, "truncated": len(matches) > 100,
    }
