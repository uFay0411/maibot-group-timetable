"""真实课表的文字与分页 PNG 输出；字体随插件分发，无浏览器依赖。"""

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import re

from PIL import Image, ImageDraw, ImageFont

from .query import query_schedule

FONT_PATH = Path(__file__).parent / "assets" / "NotoSansCJKsc-Regular.otf"


def command_query(store, owner: str, args: list[str], now=None) -> dict:
    args = list(args) or ["今天"]
    action, *values = args
    subject, days = "", 1
    if action == "查询" and 1 <= len(values) <= 2:
        period = values[0]
        days = int(values[1]) if len(values) == 2 else 1
    elif action == "搜索" and 1 <= len(values) <= 3:
        subject = values[0]
        period = values[1] if len(values) > 1 else "未来31天"
        days = int(values[2]) if len(values) == 3 else 1
    elif len(values) <= 1:
        period = action
        days = int(values[0]) if values else 1
    else:
        raise ValueError("用法：图片 [今天|明天|本周|YYYY-MM-DD] [天数1~31]")
    return query_schedule(store, owner, period, days, subject, "全天", now)


def format_query(result: dict, name: str) -> str:
    lines = [f"{name}的课表 · {result['timezone']} · {result['range_start'][:10]}"]
    if not result["lessons"]:
        return lines[0] + "\n所查范围内没有符合条件的课程。"
    for row in result["lessons"][:60]:
        start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        lines.append(f"{start:%m-%d %H:%M}–{end:%H:%M} {row['subject']} ｜{row['classroom'] or '教室未填写'}\n课次 {row['lesson_id']}")
    if result["total"] > 60:
        lines.append(f"还有 {result['total'] - 60} 节，请缩小日期范围。")
    return "\n".join(lines)


def wrapped(draw, text: str, font, width: int, max_lines: int = 3) -> list[str]:
    text = " ".join(str(text).split())[:1000]
    lines, current = [], ""
    atoms = re.findall(r"[A-Za-z0-9+#]+|.", text)
    tokens = [part for atom in atoms for part in
              (list(atom) if draw.textlength(atom, font=font) > width else [atom])]
    for char in tokens:
        if draw.textlength(current + char, font=font) > width and current:
            lines.append(current.rstrip())
            current = ""
            if len(lines) == max_lines:
                while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
                    lines[-1] = lines[-1][:-1]
                lines[-1] += "…"
                return lines
        current += char.lstrip() if not current else char
    if current or not lines:
        lines.append(current)
    return lines


def render_timetable(result: dict, name: str) -> list[bytes]:
    if result.get("requires_course_selection") or "lessons" not in result:
        raise ValueError("请先确定科目并查询真实课次，再生成图片")
    lower = datetime.fromisoformat(result["range_start"])
    upper = datetime.fromisoformat(result["range_end_exclusive"])
    day_count = (upper.date() - lower.date()).days
    if not 1 <= day_count <= 31 or len(result["lessons"]) > 100:
        raise ValueError("图片仅支持1~31天、最多100条已返回课次")
    if not FONT_PATH.is_file():
        raise ValueError("课表中文字体缺失，请完整更新插件 assets 目录")
    fonts = {size: ImageFont.truetype(str(FONT_PATH), size) for size in (22, 25, 28, 38)}
    width, max_height = 1080, 2100
    bg, ink, muted, accent = "#F1F5F9", "#172B42", "#586D80", "#256B91"
    pages, image, draw, y = [], None, None, 0

    def new_page():
        nonlocal image, draw, y
        image = Image.new("RGB", (width, max_height), bg)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((28, 25, 1052, 177), radius=22, fill=ink)
        title = wrapped(draw, name + "的课表", fonts[38], 940, 1)[0]
        draw.text((58, 43), title, font=fonts[38], fill="white")
        range_label = f"{lower:%Y-%m-%d} — {upper - timedelta(days=1):%Y-%m-%d}  ·  {result['timezone']}"
        draw.text((60, 114), range_label, font=fonts[22], fill="#CFDFEB")
        y = 201

    def finish():
        if image is None:
            return
        footer = f"第 {len(pages) + 1} 页  ·  课表查询结果"
        if result.get("truncated"):
            footer += f"  ·  仅展示前 {len(result['lessons'])}/{result['total']} 节，请缩小范围"
        draw.text((44, y + 18), footer, font=fonts[22], fill=muted)
        out = BytesIO()
        image.crop((0, 0, width, min(max_height, y + 82))).save(out, format="PNG", optimize=True)
        pages.append(out.getvalue())

    by_day = {}
    for row in result["lessons"]:
        by_day.setdefault(datetime.fromisoformat(row["start"]).date(), []).append(row)
    new_page()
    for offset in range(day_count):
        day = lower.date() + timedelta(days=offset)
        rows = sorted(by_day.get(day, []), key=lambda row: row["start"])

        def day_header(continued=False, day=day):
            nonlocal y
            label = f"{day:%m月%d日}  周{'一二三四五六日'[day.weekday()]}" + (" · 续" if continued else "")
            draw.rounded_rectangle((34, y, 1046, y + 56), radius=10, fill="#DDEBF2")
            draw.text((54, y + 7), label, font=fonts[28], fill=accent)
            y += 66

        if y + 240 > max_height - 82:
            finish()
            new_page()
        day_header()
        if not rows:
            filtered = (result.get("subject") or result.get("subject_resolution") or result.get("time_of_day") != "全天"
                        or result.get("period") in {"下一节", "下一次早八"})
            text = "当天无符合筛选条件的课" if filtered else "当天无课"
            if result.get("truncated"):
                text = "当天未展示课次（查询结果已截断）"
            draw.text((55, y + 4), text, font=fonts[25], fill=muted)
            y += 64
            continue
        for row in rows:
            titles = wrapped(draw, row["subject"], fonts[28], 550)
            rooms = wrapped(draw, row["classroom"] or "教室未填写", fonts[25], 225)
            height = max(96, len(titles) * 42 + 22, len(rooms) * 38 + 22)
            if y + height > max_height - 82:
                finish()
                new_page()
                day_header(True)
            draw.rounded_rectangle((34, y, 1046, y + height - 9), radius=12, fill="white")
            start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
            draw.text((54, y + 10), start.strftime("%H:%M"), font=fonts[28], fill=ink)
            draw.text((54, y + 49), end.strftime("%H:%M"), font=fonts[22], fill=muted)
            for index, line in enumerate(titles):
                draw.text((208, y + 9 + index * 42), line, font=fonts[28], fill=ink)
            for index, line in enumerate(rooms):
                draw.text((795, y + 12 + index * 38), line, font=fonts[25], fill=accent)
            y += height
        y += 15
    finish()
    return pages


def render_group_current(result: dict, name: str = "") -> list[bytes]:
    """保留服务端的下课时间排序，不重新按上课日期/开始时间分组。"""
    if result.get("query_type") != "group_current" or len(result.get("lessons", [])) > 50:
        raise ValueError("请使用有效的群内当前上课查询结果生成图片")
    if not FONT_PATH.is_file():
        raise ValueError("课表中文字体缺失，请完整更新插件 assets 目录")
    fonts = {size: ImageFont.truetype(str(FONT_PATH), size) for size in (22, 25, 28, 38)}
    at = datetime.fromisoformat(result["queried_at"])
    pages, image, draw, y = [], None, None, 0

    def new_page():
        nonlocal image, draw, y
        image = Image.new("RGB", (1080, 2100), "#F1F5F9")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((28, 25, 1052, 215), radius=22, fill="#172B42")
        draw.text((58, 44), "群内当前正在上课", font=fonts[38], fill="white")
        draw.text((60, 110), f"{at:%Y-%m-%d %H:%M:%S} · {result['timezone']}", font=fonts[22], fill="#CFDFEB")
        draw.text((60, 152), f"{result['members_in_class']}位群友 · 按下课时间从早到晚", font=fonts[25], fill="#CFDFEB")
        draw.text((54, 236), "下课 / 开始", font=fonts[22], fill="#586D80")
        draw.text((230, 236), "群友 / 科目", font=fonts[22], fill="#586D80")
        draw.text((795, 236), "教室", font=fonts[22], fill="#586D80")
        y = 279

    def finish():
        text = (f"按已建立课表统计 {result['members_with_timetable']}/{result['group_member_count']}人"
                f" · 结果{result['page']}/{result['total_pages']}页 · 图片{len(pages)+1}")
        draw.text((44, y + 16), text, font=fonts[22], fill="#586D80")
        output = BytesIO()
        image.crop((0, 0, 1080, min(2100, y + 82))).save(output, format="PNG", optimize=True)
        pages.append(output.getvalue())

    new_page()
    for row in result["lessons"]:
        title = row["subject"] + (" [课表时间冲突]" if row.get("conflicting_lessons") else "")
        titles = wrapped(draw, title, fonts[28], 520)
        rooms = wrapped(draw, row["classroom"] or "教室未填写", fonts[25], 225)
        height = max(128, len(titles) * 42 + 58, len(rooms) * 38 + 22)
        if y + height > 2018:
            finish()
            new_page()
        draw.rounded_rectangle((34, y, 1046, y + height - 9), radius=12, fill="white")
        start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
        draw.text((54, y + 10), end.strftime("%H:%M"), font=fonts[28], fill="#256B91")
        draw.text((54, y + 51), end.strftime("%m-%d") if end.date() != at.date() else "下课", font=fonts[22], fill="#586D80")
        draw.text((54, y + 84), start.strftime("%H:%M") + "开始", font=fonts[22], fill="#586D80")
        display = wrapped(draw, f"{row['member_name']} ({row['user_id']})", fonts[22], 520, 1)[0]
        draw.text((230, y + 8), display, font=fonts[22], fill="#586D80")
        for index, line in enumerate(titles):
            draw.text((230, y + 44 + index * 42), line, font=fonts[28], fill="#172B42")
        for index, line in enumerate(rooms):
            draw.text((795, y + 15 + index * 38), line, font=fonts[25], fill="#256B91")
        y += height
    if not result["lessons"]:
        draw.text((54, y + 20), "已建立课表的群友当前没有正在上的课", font=fonts[28], fill="#586D80")
        y += 104
    finish()
    return pages
