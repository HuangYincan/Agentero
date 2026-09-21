# Agentero 上下文细则

## 1. 图从哪里来

### 论文插图（推荐路径）

版面分析会为每篇论文写出 `{paper}/source/layout-index.json`，其中每条区域含 `id`（如 `figure-3`）、`kind`、`page`（1-based）、`bbox`（0–1 页相对）：

```bash
agentero layout list papers/1706.03762 --kind figure --json
agentero layout get  papers/1706.03762 figure-3 --json
```

`layout_index_missing` 表示这篇论文还没跑过版面分析 —— 请在 App 里打开论文触发一次，或直接走"PDF 整页"路径。

### PDF 整页

```bash
pdftoppm -r 200 -f 8 -l 8 -png -singlefile papers/1706.03762/1706.03762.pdf page8
```

- `-singlefile` 保证输出名可预期；`-r 200` 是常用的证据分辨率（版面分析服务同样按 200 DPI 估算）。
- 若图中有细线或小字号刻度，可以提高到 `-r 300`；改分辨率等于换了一张测量面，spec 必须重新 `inspect`。
- **切面板**用 `figure_digitizer.py crop`（原生分辨率切割），不要用截图或缩放。

### 视觉批注裁剪

用户在阅读器里框选的区域落在：

```
{paper}/marks/assets/<mark-id>.png    # 裁剪图（最长边 1600 px）
{paper}/marks/<mark-id>.json          # 区域、页面、备注、可选会话
```

这条路径的裁剪图是**上游产物**：它可能已被缩放，因此默认只作参考。要用它做测量面，先在覆盖图里确认刻度线是否还清晰可辨；不确定就回到 PDF 页重新 `crop`。

## 2. 产物落盘

```
{paper}/source/digitize/<figure-id>/
├── data.csv
├── overlay.png
├── recreated.png
├── report.json
└── figure-spec.json
```

- 多面板各建一个目录（`figure-3a/`、`figure-3b/`），不要把两个坐标系混进同一份 spec。
- 中间文件（页图、面板裁剪、`*.crop.json`）放同一目录，不要散落到 Vault 根。
- 产物是普通文件，可以直接被 `@` 进对话、写进 `NOTES.md` 的 wikilink，或被 CLI 读取。

## 3. 回写 Vault

| 目的 | 做法 |
|---|---|
| 把结论钉回图上 | `agentero mark add {paper} --region figure-3 --comment "抽取结果与源数据一致，见 source/digitize/figure-3" --json` |
| 在笔记里引用 | 沿用论文阅读的引用格式：`[Figure 3](papers/<id>/<id>.pdf#figure=3)`；产物路径写相对路径 `papers/<id>/source/digitize/figure-3/data.csv` |
| 标注已读 | 仅当用户要求或本流程确实完成了该论文的阅读时才 `agentero paper set-read {paper} --json`；单纯数字化一张图不算读完论文 |

写进 `NOTES.md` 的数值结论必须带证据等级（直接测量 / 描迹 / 重拟合 / 低置信），不要只丢一张表。

## 4. 远程 Vault

远程 Vault 上 `source/`、`marks/` 的写入走 bridge，路径语义与本机一致，但**大图与批处理会明显变慢**。远程场景下建议：

- 先在本地把 spec 与参数调好，再在远程跑一次；
- 避免把整本 PDF 的高分辨率页图反复落盘。

## 5. 隐私与外部服务

本 Skill 的全部脚本只做本地像素运算，不联网、不调用任何模型。以下情况必须**先取得用户明确同意**，并在 `report.json` 的 `notes` 中写明：

- 把图发给远端 OCR / 视觉模型辅助判读；
- 把图上传到第三方服务做版面识别。

即便如此，远端结果也只能作为**提案**（例如"这看起来像对数轴"），最终数值仍必须来自本地在原始栅格上的标定测量。

## 6. 依赖

```bash
python -m pip install numpy pillow
```

`pdftoppm`（poppler）仅在做 PDF → 页图时需要。脚本对缺失依赖会直接报错，不会静默降级。
