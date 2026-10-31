# PDF 报告的内置字体

`NotoSansSC-Regular.ttf` 是账单深度分析 PDF 唯一使用的字体。它随代码入库，
而不是去读操作系统的字体目录 —— 之前硬编码 `C:/Windows/Fonts/simhei.ttf`，
在 Linux 容器里找不到，注册失败又被 `except: pass` 吞掉，结果是「发邮件」在
真机上必然失败（报告生成不出来）。字体进仓库后，Windows、Linux、macOS、
Docker、CI 用的是同一份字形，排版也一致。

## 来源与许可

- 上游：Noto Sans SC（Google Fonts）<https://github.com/google/fonts/tree/main/ofl/notosanssc>
- 版本：可变字体 `NotoSansSC[wght].ttf`，2026-10-27 取用
- 许可：SIL Open Font License 1.1，全文见同目录 `OFL.txt`。允许嵌入与随软件再分发。

## 文件是裁剪过的

上游可变字体 17.7 MB，仓库里这份是静态常规字重（`wght=400`）再按字符范围裁剪的
7.4 MB 版本。保留的码位：

```
U+0020-007E  ASCII（数字、英文、半角标点）
U+00A0-00FF  Latin-1（° 这类符号）
U+2000-206F  通用标点（— – ‘ ’ “ ” …）
U+20A0-20CF  货币符号（¥）
U+2190-21FF  箭头
U+2460-24FF  带圈数字
U+2500-25FF  制表符与几何图形
U+3000-303F  CJK 标点（、。《》）
U+4E00-9FFF  CJK 统一表意文字（20992 字，覆盖常用与多数生僻姓氏）
U+FF00-FFEF  全角形式（￥ 等）
```

**不要**进一步裁成 GB2312 子集：报告里的对手方、商品名是用户数据，生僻字一旦缺失
就会印成方框。真要重新裁剪，保持上面的范围即可。

## 重新生成

```bash
# 1. 取上游可变字体与许可证
curl -L -o NotoSansSC-VF.ttf \
  "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
curl -L -o OFL.txt \
  "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt"

# 2. 实例化为常规字重
uv run --with fonttools fonttools varLib.instancer NotoSansSC-VF.ttf wght=400 \
  --output NotoSansSC-Regular-static.ttf

# 3. 按上面的码位裁剪（--layout-features="" 去掉 GSUB/GPOS，reportlab 不做复杂排版）
uv run --with fonttools pyftsubset NotoSansSC-Regular-static.ttf \
  --output-file NotoSansSC-Regular.ttf \
  --unicodes="U+0020-007E,U+00A0-00FF,U+2000-206F,U+20A0-20CF,U+2190-21FF,U+2460-24FF,U+2500-25FF,U+3000-303F,U+4E00-9FFF,U+FF00-FFEF" \
  --layout-features="" --name-IDs="1,2,3,4,6" --no-hinting
```

重新生成后跑一遍 `backend/tests/test_analysis_pdf.py`：那里有一条用例会直接断言
字体能注册、能出 PDF。
