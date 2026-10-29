"""参考群友课程看板：圆形头像、箭头、状态色块与相对时间。"""

from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

COLORS = {"in_class": ("#B73B45", "#E9BFC3"), "next": ("#424CAE", "#BDC1E2"),
          "finished": ("#399566", "#BBDDCB"), "free": ("#687B87", "#D9E1E5")}


def duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours}小时" + (f"{rest}分钟" if rest else "")


def render_board(result: dict, font_path, wrap, avatars: dict[str, bytes] | None = None) -> list[bytes]:
    fonts = {size: ImageFont.truetype(str(font_path), size) for size in (22, 25, 28, 33, 36, 45, 64)}
    at = datetime.fromisoformat(result["queried_at"])
    rows, pages = result["lessons"], []
    # 每张最多9行，页脚保留覆盖范围与分页，避免多人时截断。
    for offset in range(0, max(1, len(rows)), 9):
        chunk = rows[offset:offset + 9]
        height = 278 + max(1, len(chunk)) * 196 + 140
        image = Image.new("RGB", (1080, height), "white")
        draw = ImageDraw.Draw(image)
        teal = "#00897D"
        draw.polygon([(26, 26), (134, 26), (134, 80), (80, 80), (80, 134), (26, 134)], fill=teal)
        draw.rectangle((108, 179, 972, 210), fill="#ADD8D2")
        draw.text((125, 77), "“群友在上什么课？”", font=fonts[64], fill="#111111")
        summary = (f"进行中 {result['members_in_class']} · 下一节 {result['members_with_next']} · "
                   f"已结束 {result['members_finished']} · 今日无课 {result['members_free_today']}")
        draw.text((126, 224), summary, font=fonts[22], fill="#627270")
        y = 278
        for row in chunk:
            status = row["status"]
            color, pale = COLORS[status]
            cx, cy, size = 130, y + 15, 132
            tile = Image.new("RGB", (size, size), pale)
            raw = (avatars or {}).get(row["user_id"])
            if raw:
                with Image.open(BytesIO(raw)) as avatar:
                    tile = ImageOps.fit(avatar.convert("RGB"), (size, size))
            else:
                d = ImageDraw.Draw(tile)
                initial = ("".join(str(row["member_name"]).split()) or "?")[0]
                d.text((size / 2, size / 2 - 4), initial, font=fonts[64], fill=color, anchor="mm")
            mask = Image.new("L", (size, size))
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            image.paste(tile, (cx, cy), mask)
            draw.polygon([(265, y + 31), (306, y + 31), (345, y + 82), (306, y + 133),
                          (265, y + 133), (304, y + 82)], fill=pale)
            draw.polygon([(357, y + 18), (372, y + 30), (357, y + 42)], fill="#111111")
            name = wrap(draw, row["member_name"], fonts[45], 580, 1)[0]
            draw.text((394, y - 3), name, font=fonts[45], fill="#111111")
            draw.rectangle((355, y + 72, 518, y + 132), fill=color)
            label = row["status_label"]
            draw.text((436, y + 98), label, font=fonts[33 if len(label) > 3 else 36], fill="white", anchor="mm")
            if status in {"in_class", "next"}:
                subject = wrap(draw, row["subject"], fonts[33], 483, 1)[0]
                draw.text((533, y + 61), subject, font=fonts[33], fill=color)
                start, end = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])
                start_label = start.strftime("%H:%M" if start.date() == at.date() else "%m-%d %H:%M")
                end_label = end.strftime("%H:%M" if end.date() == at.date() else "%m-%d %H:%M")
                clock = f"{start_label}–{end_label}"
                countdown = ("剩余 " + duration(row["remaining_minutes"]) if status == "in_class" else
                             duration(row["starts_in_minutes"]) + "后")
                # 时间独占一行；跨日课程也不会与教室或倒计时互相覆盖。
                draw.text((533, y + 108), clock, font=fonts[25], fill=color)
                details = f"{countdown} · {row['classroom'] or '教室未填写'}"
                if row.get("conflicting_lessons"):
                    details += " · 时间冲突"
                draw.text((355, y + 148), wrap(draw, details, fonts[25], 660, 1)[0], font=fonts[25], fill=color)
            else:
                title = "今日课程已上完" if status == "finished" else "今天没有安排课程"
                draw.text((533, y + 61), title, font=fonts[33], fill=color)
                if status == "finished":
                    draw.text((533, y + 108), "共计 " + duration(row["completed_minutes"]), font=fonts[25], fill=color)
            y += 196
        if not chunk:
            draw.text((130, y + 58), "本群暂无已建立的课表", font=fonts[36], fill="#687B87")
            y += 196
        footer = f"mai课表 · {at:%Y/%m/%d %H:%M:%S} · {result['timezone']}"
        draw.text((540, y + 8), footer, font=fonts[22], fill="#364340", anchor="mt")
        scope = (f"仅本群已建课表 {result['members_with_timetable']} 人 · 未核实离群状态"
                 if result.get("roster_verified") is False else f"仅显示本群已建课表 {result['members_with_timetable']} 人")
        draw.text((540, y + 45), scope, font=fonts[22], fill="#627270", anchor="mt")
        page = f"结果 {result['page']}/{result['total_pages']} 页 · 图片 {len(pages) + 1}/{max(1, (len(rows) + 8) // 9)}"
        draw.text((540, y + 80), page, font=fonts[22], fill="#627270", anchor="mt")
        draw.polygon([(946, height - 26), (1054, height - 26), (1054, height - 134),
                      (1000, height - 134), (1000, height - 80), (946, height - 80)], fill=teal)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        pages.append(output.getvalue())
    return pages
