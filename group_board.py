"""mai课表课程卡片：清晰的信息层级、状态标签与相对时间。"""

from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

COLORS = {"in_class": ("#AC5B30", "#FAEBDD"), "next": ("#316B67", "#E5F1ED"),
          "finished": ("#667286", "#EDF0F5"), "free": ("#858476", "#F1F0E9")}
ROWS_PER_IMAGE = 8


def duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours}小时" + (f"{rest}分钟" if rest else "")


def render_board(result: dict, font_path, wrap, avatars: dict[str, bytes] | None = None) -> list[bytes]:
    fonts = {size: ImageFont.truetype(str(font_path), size) for size in (20, 22, 25, 28, 34, 36, 46)}
    at = datetime.fromisoformat(result["queried_at"])
    rows, pages = result["lessons"], []
    total_images = max(1, (len(rows) + ROWS_PER_IMAGE - 1) // ROWS_PER_IMAGE)
    ink, muted, background = "#283E38", "#78857D", "#F4F3ED"
    for offset in range(0, max(1, len(rows)), ROWS_PER_IMAGE):
        chunk = rows[offset:offset + ROWS_PER_IMAGE]
        height = 308 + max(1, len(chunk)) * 212 + 110
        image = Image.new("RGB", (1080, height), background)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((44, 40, 84, 80), radius=12, fill="#3B6557")
        draw.line((55, 62, 63, 69, 74, 53), fill="white", width=4)
        draw.text((100, 43), "mai课表 / 群内动态", font=fonts[25], fill="#3B6557")
        draw.text((44, 98), "群友课程一览", font=fonts[46], fill=ink)
        draw.text((46, 172), "根据已导入课表更新 · 进行中按下课时间排序", font=fonts[22], fill=muted)
        draw.text((1034, 87), f"{at:%H:%M}", font=fonts[46], fill=ink, anchor="rt")
        date = f"{at:%m月%d日}  周{'一二三四五六日'[at.weekday()]}"
        draw.text((1034, 151), date, font=fonts[22], fill=muted, anchor="rt")
        stats = [("in_class", "正在上课", result['members_in_class']),
                 ("next", "今日下一节", result['members_with_next']),
                 ("finished", "今日已结束", result['members_finished']),
                 ("free", "今日无课", result['members_free_today'])]
        for index, (status, label, count) in enumerate(stats):
            x = 44 + index * 252
            color, pale = COLORS[status]
            draw.rounded_rectangle((x, 222, x + 236, 286), radius=16, fill=pale)
            draw.text((x + 18, 237), label, font=fonts[22], fill=color)
            draw.text((x + 218, 229), str(count), font=fonts[34], fill=color, anchor="rt")
        y = 308
        for number, row in enumerate(chunk, offset + 1):
            status = row["status"]
            color, pale = COLORS[status]
            draw.rounded_rectangle((44, y + 3, 1036, y + 203), radius=22, fill="#E8E9E2")
            draw.rounded_rectangle((44, y, 1036, y + 200), radius=22, fill="white")
            draw.rounded_rectangle((44, y + 27, 50, y + 173), radius=3, fill=color)
            size, cx, cy = 82, 78, y + 26
            tile = Image.new("RGB", (size, size), pale)
            raw = (avatars or {}).get(row["user_id"])
            if raw:
                with Image.open(BytesIO(raw)) as avatar:
                    tile = ImageOps.fit(avatar.convert("RGB"), (size, size))
            else:
                initial = ("".join(str(row["member_name"]).split()) or "?")[0]
                ImageDraw.Draw(tile).text((size / 2, size / 2 - 3), initial, font=fonts[36], fill=color, anchor="mm")
            mask = Image.new("L", (size, size))
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            image.paste(tile, (cx, cy), mask)
            draw.text((119, y + 128), f"{number:02d}", font=fonts[20], fill="#B1B8AF", anchor="mt")
            name = wrap(draw, row["member_name"], fonts[28], 620, 1)[0]
            draw.text((188, y + 16), name, font=fonts[28], fill=ink)
            draw.rounded_rectangle((852, y + 21, 1008, y + 62), radius=14, fill=pale)
            draw.text((930, y + 39), row["status_label"], font=fonts[22], fill=color, anchor="mm")
            if status in {"in_class", "next"}:
                subject = wrap(draw, row["subject"], fonts[36], 812, 1)[0]
                draw.text((188, y + 63), subject, font=fonts[36], fill=ink)
                start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
                start_label = start.strftime("%H:%M" if start.date() == at.date() else "%m-%d %H:%M")
                end_label = end.strftime("%H:%M" if end.date() == at.date() else "%m-%d %H:%M")
                draw.text((188, y + 119), f"{start_label}–{end_label}", font=fonts[25], fill=muted)
                countdown = ("剩余 " + duration(row["remaining_minutes"]) if status == "in_class" else
                             duration(row["starts_in_minutes"]) + "后上课")
                details = f"{countdown} · {row['classroom'] or '教室未填写'}"
                if row.get("conflicting_lessons"):
                    details += " · 时间冲突"
                draw.text((188, y + 158), wrap(draw, details, fonts[22], 812, 1)[0], font=fonts[22], fill=color)
            else:
                title = "今日课程已结束" if status == "finished" else "今天没有安排课程"
                draw.text((188, y + 63), title, font=fonts[36], fill=ink)
                details = ("今日累计 " + duration(row["completed_minutes"]) if status == "finished" else
                           "享受课间之外的时间")
                draw.text((188, y + 121), details, font=fonts[25], fill=muted)
            y += 212
        if not chunk:
            draw.rounded_rectangle((44, y, 1036, y + 200), radius=22, fill="white")
            draw.text((540, y + 80), "本群暂无已建立的课表", font=fonts[36], fill=muted, anchor="mt")
            y += 212
        scope = (f"已建课表 {result['members_with_timetable']} 人 · 未核实离群状态"
                 if result.get("roster_verified") is False else f"仅显示已建课表的 {result['members_with_timetable']} 位群友")
        draw.text((46, y + 16), scope, font=fonts[20], fill=muted)
        draw.text((46, y + 53), f"{at:%Y-%m-%d %H:%M:%S} · {result['timezone']}", font=fonts[20], fill=muted)
        page = f"结果 {result['page']}/{result['total_pages']} 页 · 图片 {len(pages) + 1}/{total_images}"
        draw.text((1034, y + 53), page, font=fonts[20], fill=muted, anchor="rt")
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        pages.append(output.getvalue())
    return pages
