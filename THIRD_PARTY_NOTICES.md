# WakeUpDecoder

`_wakeup_protocol.py` contains an adapted subset of the pure protocol helpers from
[airline233/WakeUpDecoder](https://github.com/airline233/WakeUpDecoder), revision
`577161c9ea8c34dddbcecd6ea9a702687c4ac8c4`, licensed under Apache License 2.0.

Changes made on 2026-10-25: retained the native DES/RC4, encoding, signing and
request-construction functions; removed APK parsing, network requests, CLI and
diagnostic response dumps; added explicit `strict=False` to two `zip` calls.
The full license is included in
[`licenses/WakeUpDecoder-APACHE-2.0.txt`](licenses/WakeUpDecoder-APACHE-2.0.txt).

`wakeup.py` implements this plugin's asynchronous HTTP client, response validation,
share-data conversion and integration. Its public app compatibility profile was
extracted from the WakeUp 6.1.70 APK provided by that pinned reference repository.
No APK, private user share code, live timetable data or user/device credentials
are included in the repository or release package.

# Noto Sans CJK

`assets/NotoSansCJKsc-Regular.otf` is the unmodified Noto Sans CJK SC Regular font
from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk), revision
`f8d157532fbfaeda587e826d4cd5b21a49186f7c`, path
`Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf`.
Copyright notices remain in the original font metadata. It is distributed under
the SIL Open Font License 1.1, included at `licenses/NotoSansCJK-OFL.txt`.
The font is bundled for Chinese timetable rendering on Windows and Linux.

`examples/timetable-preview.png` uses only synthetic demonstration data.
