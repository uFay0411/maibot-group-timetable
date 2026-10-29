"""从随包manifest与更新日志读取版本；帮助/日志默认发送可缩放图片。"""

from pathlib import Path

import json
import re

ROOT = Path(__file__).resolve().parent
HELP_CARD = """查询
/课表 今天｜明天｜本周｜下周
/课表 图片 明天；/课表 搜索 高数 本周
/课表 群内当前 [图片]；/课表 群友接下来
/课表 群友 @成员 明天 [图片]

导入
上传自己的ICS，或引用文件后发送 /课表 解析
/课表 wakeup 分享口令
带原图发送 /课表 图片导入 补充说明
周课表请说明学期首周日期、周次和节次钟点。
/课表 文字导入 日期、时间、科目、教室
识别先预览，再用 /课表 确认 草稿编号 保存。

维护与提醒
可以直接说：把明天的高数调到后天10点。
也能说：明天英语停一次、关闭我的课表提醒。
/课表 提醒 开｜关；/课表 状态
/课表 管理 @成员 …（仅MaiBot管理员）

帮助与版本
/课表 版本；/课表 更新日志 [版本号]
/课表 帮助 完整；/课表 帮助 文字
课表按当前群与成员分别保存。"""


def version_text(selected: str = "") -> str:
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    matches = list(re.finditer(r"^# (\d+\.\d+\.\d+)\s*$", changelog, flags=re.MULTILINE))
    blocks = [(m.group(1), changelog[m.start():matches[i+1].start() if i+1 < len(matches) else len(changelog)].strip())
              for i, m in enumerate(matches)]
    if selected:
        blocks = [item for item in blocks if item[0] == selected]
        if not blocks:
            raise ValueError("随包更新日志中没有这个版本，请使用 /课表 版本 查看当前版本")
    else:
        blocks = blocks[:1]
    return (f"{manifest['name']}\n当前版本：{manifest['version']}\n"
            f"MaiBot：{manifest['host_application']['min_version']}～{manifest['host_application']['max_version']}\n"
            f"SDK：{manifest['sdk']['min_version']}～{manifest['sdk']['max_version']}\n\n"
            + "\n\n".join(text for _, text in blocks)
            + "\n\n指定旧版本：/课表 更新日志 1.10.4\n可复制文字：/课表 版本 文字")


def info_request(body: str, full_help: str):
    args = body.split()
    if not args or args[0] == "帮助":
        values = args[1:]
        if any(x not in {"完整", "文字", "图片"} for x in values):
            raise ValueError("用法：/课表 帮助 [完整] [文字]")
        return "帮助", full_help if "完整" in values else HELP_CARD, "文字" not in values
    if args[0] not in {"版本", "更新日志"}:
        return None
    values = [x for x in args[1:] if x not in {"文字", "图片", "更新日志", "日志"}]
    if len(values) > 1 or (values and re.fullmatch(r"\d+\.\d+\.\d+", values[0]) is None):
        raise ValueError("用法：/课表 版本 [文字] 或 /课表 更新日志 [版本号] [文字]")
    return "版本与更新日志", version_text(values[0] if values else ""), "文字" not in args
