# 抽取契约：spec、report 与状态词表

本文件是 `figure-digitizer` 的数据契约。脚本按它执行，人按它复核。字段名以脚本输出的 JSON 为准。

## 1. 四种 schema

| schema | 由谁写出 | 用途 |
|---|---|---|
| `agentero.figure-digitizer/spec/v1` | `inspect --output-spec`，人填 | 测量指令：测量面、绘图区、标定、序列 |
| `agentero.figure-digitizer/preflight/v1` | `inspect --output-report` | 输入构成与路线提案，**永不授权数值** |
| `agentero.figure-digitizer/crop/v1` | `crop` | 面板裁剪的来源记录（父页身份 + bbox + 偏移） |
| `agentero.figure-digitizer/report/v1` | `extract` | 运行证据：标定、参数、覆盖、拒绝原因、授权结论 |

## 2. spec 字段

```jsonc
{
  "schema": "agentero.figure-digitizer/spec/v1",
  "source": { "path": "...", "sha256": "...", "width": 0, "height": 0 },
  "figure": {
    "paper": "papers/1706.03762",
    "figure_id": "figure-3",
    "page": 8,                  // 1-based，PDF 页码
    "panel": "b",               // 多面板图里的单个面板
    "chart_type": "line",       // 必须落在注册表的 chart_types 里
    "route": "line",            // 通常由 inspect 填好
    "source_crop": "fig3b.png.crop.json",  // 裁剪而来时填，用于回溯父页
    "verified": true,           // 人已确认面板/轴/序列
    "verified_by": "user"
  },
  "plot_bounds": [61, 31, 480, 330],   // 绘图区像素矩形，不含轴标题/图例/面板字母
  "calibration": {
    "x": { "scale": "linear", "anchors": [[60, 0], [480, 10]], "verified": true },
    "y": { "scale": "log10",  "anchors": [[330, 0.1], [30, 100]], "verified": true }
  },
  "series": [ { "name": "treatment", "color": "#d62728", "tolerance": 40 } ],
  "options": {
    "baseline_value": 0,        // bar 路线必填：值轴上的基线读数
    "exclude_regions": [[l, t, r, b]],
    "min_height_px": 2,
    "max_gap_px": 1,
    "merge_gap_px": 0,
    "min_component_px": 6,
    "max_component_px": 400,
    "max_step_px": null,        // line 路线：限定相邻列的最大跳变
    "residual_limit": 0.01      // 标定残差上限（占轴跨度比例）
  }
}
```

要点：

- `anchors` 是 `[像素, 数值]`，像素取**原始栅格**坐标（左上原点）。轴为 `log10` 时数值必须为正，拟合在 `log10(value)` 空间做。
- 多于两个锚点时做最小二乘，残差写进 report；残差超限直接阻塞授权 —— 那说明锚点读错了。
- `color` 取序列本体色；抗锯齿边缘色会把掩膜拖宽。
- `tolerance` 是 RGB 欧氏距离阈值。写之前先用 `inspect` 的 `dominant_colors` 核对一下主色。

## 3. report 字段

| 字段 | 含义 |
|---|---|
| `source` | 本次测量面的身份（SHA-256 + 宽高） |
| `figure` | 回抄 spec 的面板信息，含 `source_crop` |
| `route` | `{id, maturity}`；成熟度以注册表为准 |
| `plot_bounds` | 实际使用的绘图区 |
| `calibration.{x,y}` | 锚点、变换、`max_data_residual` |
| `series[]` | 每个序列的 `status`、样本数、缺口列数与缺口区间、被拒组件数 |
| `diagnostics` | 路线专属诊断，例如 `unresolved_conflicts`、`measured_bins`、`baseline_pixel` |
| `numeric_output_authorized` | 是否授权把数值当作可用结论 |
| `authorization_blockers[]` | 未授权时逐条写明原因 |
| `status` | 本次运行的整体状态（见下） |
| `artifacts` | `data.csv` / `overlay.png` / `recreated.png` 的路径与统计 |
| `notes` | 路线级提醒（例如"缺口不插值"） |

## 4. 状态词表

| 状态 | 含义 |
|---|---|
| `extracted` | 该序列/整体全部可见图元都已测量并复核 |
| `partial_visible` | 部分可见：有缺口、遮挡或部分组件被拒 |
| `low_confidence` | 有测量结果但过不了质量门，只能作为线索 |
| `not_extracted` | 没有产出数值 |
| `refused` | 主动拒绝（坐标系不可标定、图元不可分离等） |
| `occluded_by_overlay` | 单元级状态：该柱/段被别的前景覆盖，数值仍可读但基线/端点被挡 |

`gap`（缺口）与 `not_extracted` 的含义都是**图上没有可测的像素**，绝不等价于 0。

## 5. 授权门

`numeric_output_authorized` 为 true 需要同时满足：

1. `figure.verified` 为 true（面板、轴、目标序列已被确认）；
2. 两轴标定残差都在 `residual_limit` 内；
3. `diagnostics.unresolved_conflicts` 为 0（没有待裁决的歧义标记）；
4. 路线成熟度不是 `assisted`。

脚本在读取任何像素前就会因为 `figure.verified=false` 而拒绝执行 —— 未经确认的 spec 不是一次抽取请求。

## 6. CSV 列

| 路线 | 列 |
|---|---|
| `line` | `series,x,y,pixel_x,pixel_y,support,status` |
| `histogram` | `series,bin_index,bin_start,bin_end,height,pixel_left,pixel_right,pixel_top,pixel_bottom,status` |
| `bar` | `series,bar_index,value,pixel_left,pixel_right,pixel_top,pixel_bottom,status` |
| `scatter` | `series,x,y,pixel_x,pixel_y,size_px,fill_ratio,status` |

`pixel_*` 一律是原始栅格像素，保留它是为了任何数字都能回到图上复核。

## 7. 产物目录

默认落在论文目录下：

```
{paper}/source/digitize/<figure-id>/
├── data.csv          # 冻结的抽取表，先于任何复核产物写出
├── overlay.png       # 原分辨率覆盖图：接受(品红) / 拒绝(橙叉) / 锚点(绿十字) / 绘图区(蓝框)
├── recreated.png     # 用抽取值经标定反算回像素的复演图，检查结构与坐标一致性
├── report.json       # 运行证据
└── figure-spec.json  # 本次 spec（填实后的版本）
```

`recreated.png` 是**像素复演**，不是重绘出版级图：它只回答"标定和抽取是否自洽"。它不是排版证据，也不能当作原图的替代品。
