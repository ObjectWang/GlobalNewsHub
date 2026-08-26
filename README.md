# GlobalNewsHub

全球新闻聚合桌面应用——内嵌 RSSHub 与 BERT 分类模型，自动抓取、分类、存储国内外新闻，提供离线阅读体验。

## 功能特性

- 多来源聚合：原生 RSS 直连 + 内嵌 RSSHub 路由，公共实例三级降级兜底（§4.3）
- 智能分类：roBERTa INT8 量化模型置信度路由 + 关键词规则二次校验，栏目/地区双维度
- 三栏界面：侧边栏筛选 / 虚拟滚动列表（1000 条 ≥30fps）/ 富文本详情，明暗主题即时切换
- 全文检索：SQLite FTS5 trigram 中文搜索，输入防抖实时过滤
- 离线可用：本地 SQLite（WAL）存储，断网后可浏览全部已缓存内容
- 源健康管理：连续失败自动禁用、状态徽章、手动恢复
- 后台线程：抓取/查询/推理全在后台线程，UI 永不阻塞

## 技术栈

| 层 | 技术 |
| ---- | ---- |
| GUI | PySide6 ≥6.5（LGPL，动态链接） |
| 数据库 | SQLite ≥3.35 WAL + FTS5（trigram 中文分词） |
| 网络 | aiohttp + feedparser（异步，每主机 3s 礼貌限速） |
| 推理 | ONNX Runtime INT8 动态量化（CPU） |
| 分类模型 | hfl/chinese-roberta-wwm-ext 微调，test macro F1 = 0.985 |
| RSSHub | pkg 打包独立二进制（无 Node.js 运行时依赖） |
| 定时任务 | APScheduler ≥3.10 |

## 快速开始

### 方式一：直接运行源码

```bash
git clone https://github.com/ObjectWang/GlobalNewsHub.git
cd GlobalNewsHub
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Linux/Mac: pip install -r requirements.txt
.venv\Scripts\python main.py
```

> 首次运行自动建库建表；模型与 tokenizer 已随仓库提供
> （`resources/models/`），无需额外下载。

### 方式二：打包为独立可执行程序

```bash
# 需先构建 RSSHub 二进制（一次性，需 Node/git）：bash scripts/build_rsshub.sh
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller GlobalNewsHub.spec --noconfirm
dist\GlobalNewsHub\GlobalNewsHub.exe
```

产物为 onedir 目录（含 RSSHub 二进制 + INT8 模型）；压缩包约 155MB，
满足 §1.3 ≤300MB 安装包指标。冒烟自检：`set QT_QPA_PLATFORM=offscreen && dist\GlobalNewsHub\GlobalNewsHub.exe --smoke`

## 训练自己的分类模型（可选）

```bash
pip install -e .[train]
python scripts/preprocess_thucnews.py --data-dir <THUCNews> --out-dir data/processed
python scripts/train_classifier.py --train data/processed/train.jsonl \
    --val data/processed/val.jsonl --test data/processed/test.jsonl \
    --tokenizer-dir resources/models/tokenizer --out-dir data/checkpoints/category
python scripts/export_onnx.py --checkpoint data/checkpoints/category \
    --tokenizer-dir resources/models/tokenizer --out-dir resources/models --name category_classifier
```

训练/导出脚本内置 F1≥0.85 与 INT8≤120MB 门禁。未放置模型时应用自动
降级为关键词分类层，功能不受影响。

## 开发

```bash
.venv\Scripts\python -m pytest tests/            # 全量测试
.venv\Scripts\python -m mypy --strict core/      # 类型门禁
.venv\Scripts\python -m ruff check core/ ui/     # 风格门禁
```

进度与架构决策记录见 [AI_DEVELOPMENT_PLAN.md](AI_DEVELOPMENT_PLAN.md)。

## 分支说明

| 分支 | 用途 |
| ---- | ---- |
| `main` | 主分支,保持稳定可发布状态 |
| `dev`  | 开发分支,日常功能开发在此进行 |

## 贡献指南

1. Fork 本仓库
2. 从 `dev` 分支创建功能分支：`git checkout -b feature/xxx`
3. 提交修改并推送：`git push origin feature/xxx`
4. 向 `dev` 分支发起 Pull Request

## License

本项目代码 [MIT](LICENSE)。第三方组件许可见
[LICENSE-THIRD-PARTY](LICENSE-THIRD-PARTY)（PySide6/Qt 为 LGPL-3.0 动态链接）。
