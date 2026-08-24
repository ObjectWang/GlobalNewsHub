# GlobalNewsHub — AI 开发计划与进度

> 唯一权威输入：PRD v3.0（AI开发专用版）。本文件按 §六 的阶段记录交付状态，
> 每个 Phase 结束前必须通过 §八 质量门禁。

## 质量门禁现状（§8）

- `pytest tests/` → 82 passed（含真实 RSSHub 二进制生命周期用例）
- `mypy --strict core/` → Success（25 个源文件，零错误）
- `ruff check core/` → All checks passed

## Phase 0 环境锚定 ✅（commit 1a93266）

| ID   | 产出物                                | 状态 |
| ---- | ------------------------------------- | ---- |
| P0.1 | 完整目录树 + `__init__.py` + .gitignore | ✅   |
| P0.2 | System Prompt 模板                    | ✅   |
| P0.3 | requirements.txt + pyproject.toml     | ✅   |
| P0.4 | config/settings.yaml + sources.yaml   | ✅   |

## Phase 1 数据层 ✅（commit 458e92e）

| ID   | 产出物                                        | 验收 |
| ---- | --------------------------------------------- | ---- |
| P1.1 | core/storage/database.py + 001_init.sql       | ✅ WAL+FTS5，tests/test_database.py |
| P1.2 | core/storage/crud.py + dedup.py               | ✅ 重复URL返回已有ID，tests/test_crud.py、test_dedup.py |
| P1.3 | core/fetcher/rss_fetcher.py                   | ✅ mock源解析+限速，tests/test_rss_fetcher.py |
| P1.4 | core/utils/network.py                         | ✅ 重试/超时/UA，tests/test_network.py |
| P1.5 | MVP 源实抓验证                                | ✅ data/globalnewshub.db 已产生入库记录 |

## Phase 2 RSSHub 集成 ✅

| ID   | 产出物                                   | 验收 |
| ---- | ---------------------------------------- | ---- |
| P2.1 | scripts/build_rsshub.sh                  | ✅ Node 环境幂等执行成功；产物 resources/rsshub-server.{exe,linux,mac}（win 实测 /healthz=200） |
| P2.2 | core/rsshub/manager.py                   | ✅ start→healthz→stop 生命周期（真实二进制实测通过），tests/test_rsshub_manager.py |
| P2.3 | core/fetcher/fetch_orchestrator.py       | ✅ §4.3 降级链全覆盖，tests/test_fetch_orchestrator.py |
| P2.4 | core/fetcher/health_check.py             | ✅ 连续3次失败自动禁用，tests/test_health_check.py |

### Phase 2 关键决策（含踩坑记录）

1. **打包路线：esbuild 单文件 ESM bundle + @yao-pkg/pkg 增强 SEA 模式**。
   RSSHub 入口使用 top-level await：pkg 标准快照模式要把 ESM 转 CJS，遇到
   TLA 直接失败；只有增强 SEA 模式（--sea + package.json bin 入口，Node ≥22
   目标）按源码内置 ESM 入口。
2. **esbuild banner 契约（不可省略）**：
   `import { createRequire } ...; const require = ...; const __filename = ...; const __dirname = ...;`
   打包进来的 CJS 模块会裸调 require/__dirname；Node 22 的模块语法检测器把
   “TLA + 未定义 CJS 全局量”判定为歧义（ERR_AMBIGUOUS_MODULE_SYNTAX），
   banner 必须把三者全部定义。定位过程：source map 映射到
   simplecc-wasm 的 wasm-bindgen 胶水（`${__dirname}/simplecc_wasm_bg.wasm`）。
3. **wasm 资产必须随包**：`simplecc_wasm_bg.wasm`（按 __dirname 相对读取，
   放在入口同目录）与 `quickjs.wasm`（`new URL("../quickjs.wasm", import.meta.url)`，
   放入口上一级），均通过 package.json 的 pkg.assets 进 SEA 归档。
4. **NODE_ENV=production 是硬要求**：打包后的 RSSHub 仅在 production 模式
   加载静态路由清单；dev 模式 registry 需要扫描源码树（pkg 快照里没有）。
   bundle 时同时用 --define 把 NODE_ENV 烙成 production，死分支折叠。
5. **dist 预补丁**：dev/test 路由注册用模板字符串动态导入
   （`./routes/${ns}/${loc}`），esbuild 无法解析；脚本在打包前将其替换为
   Promise.reject（production 下永不执行，语义安全）。
6. **依赖取舍**：`--external:chromium-bidi`（patchright 的可选依赖，未安装；
   仅浏览器抓取路由需要，目标源不涉及）。
7. **网络与工具链**：pnpm ≥10 默认拦截依赖构建脚本 → tools 目录写
   .npmrc（only-built-dependencies[]=esbuild）并固定 pnpm@10.34.5；
   pkg 基座二进制走 GitHub 常停滞，脚本内置 npmmirror/ghproxy 镜像 +
   SHA256 校验的预取逻辑；minify 使 63MB bundle 降至 36MB 并规避 pkg 的
   4GB 堆限制（NODE_OPTIONS=--max-old-space-size=10240）。
8. **限速按外部主机隔离**：编排器为每个远程主机维护独立 RateLimiter
   （§2.2 的 3 秒礼仪只约束外部源）；本地 RSSHub 不限速，否则 §1.3
   “30 源 ≤ 5 秒”不可达。
9. **健康状态持久化在 source_health 表**（§2.3），内存环形缓冲保存最近 5 次
   结果供 UI 徽章；自动禁用跨重启保留，用户可 re_enable 重置。
10. **平台注意**：macOS 产物分发前需在 Mac 上 ad-hoc 签名
    （`codesign --sign - rsshub-server-mac`），脚本结尾有提示。

## Phase 3 智能分类 🚧 进行中

| ID   | 产出物                                   | 验收 |
| ---- | ---------------------------------------- | ---- |
| P3.1 | scripts/preprocess_thucnews.py           | ✅ JSONL 格式与标签映射单测 12 用例全过；合成数据 CLI 冒烟通过 |
| P3.2 | scripts/train_classifier.py              | ⏳ val F1 ≥ 0.85（需真实 THUCNews 数据集 + 训练环境） |
| P3.3 | scripts/export_onnx.py                   | ⏳ INT8 ≤ 120MB |
| P3.4 | core/classifier/pipeline.py + bert_classifier.py | ⏳ §3.2 全流程单测覆盖 |
| P3.5 | core/classifier/region_classifier.py     | ⏳ 三级策略单测覆盖 |

### P3.1 数据契约（P3.2 消费）

- 记录格式（JSONL）：`{"id","title","summary","label","source"}`；
  summary 上限 300 字（SUMMARY_MAX_CHARS），tokenizer 截断在训练/推理侧做。
- 标签映射（LABEL_MAP，10→5；military 无 THUCNews 来源，靠人工标注 --extra 补）：
  时政→politics；财经/股票/房产→economy；科技→tech；
  教育/社会/体育→life；娱乐/游戏→other。
- 均衡采样：THUCNews 各 app 标签上限 = total // 标签数（默认 10000，§3.3）；
  --extra 人工标注整体并入不下采样；80/10/10 按标签分层，固定种子可复现。
- 支持 UTF-8/GBK 双编码；未知类别目录告警跳过；正文 <20 字的退化文档丢弃。

## Phase 4 UI 层 ⏳ 未开始

P4.1 主窗口骨架 → P4.2 侧边栏 → P4.3 虚拟滚动列表 → P4.4 详情页
→ P4.5 搜索框 → P4.6 设置对话框 → P4.7 QSS 主题 → P4.8 QThread 整合。
每个组件独立会话，先信号槽契约再 UI 实现（PRD §6 Phase 4 警告）。

## Phase 5 打包发布 ⏳ 未开始

P5.1 GlobalNewsHub.spec（含 RSSHub 二进制 + ONNX 模型）→ P5.2 LICENSE-THIRD-PARTY
→ P5.3 README。

## 常用命令

```
.venv\Scripts\python -m pytest tests/          # 全量测试
.venv\Scripts\python -m mypy --strict core/    # 类型门禁
.venv\Scripts\python -m ruff check core/       # 风格门禁
bash scripts/build_rsshub.sh                   # P2.1：打包 RSSHub（需 Node/git/网络，幂等）
```

## 备注

- resources/rsshub-server* 为 scripts/build_rsshub.sh 的本地产物，不入库
  （体积各约 100-135MB）；git 中保留占位文件的决策见首次提交。
- RSSHub 构建缓存位于 build/（已被 .gitignore 覆盖）。
