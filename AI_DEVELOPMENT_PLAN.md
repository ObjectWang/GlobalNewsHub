# GlobalNewsHub — AI 开发计划与进度

> 唯一权威输入：PRD v3.0（AI开发专用版）。本文件按 §六 的阶段记录交付状态，
> 每个 Phase 结束前必须通过 §八 质量门禁。

## 质量门禁现状（§8）

- `pytest tests/` → 169 passed + 1 skipped（唯一跳过项 = region 模型门禁，
  `region_classifier_int8.onnx` 仍为占位；region 分类走域名/地名词典层不受影响。
  真实 category INT8 模型落位后，延迟<25ms / 内存<200MB 两道门禁已激活并通过）
- `mypy --strict core/` → Success（26 个源文件，零错误）
- `ruff check core/ ui/ main.py tests/` → All checks passed

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

## Phase 3 智能分类 ✅

| ID   | 产出物                                   | 验收 |
| ---- | ---------------------------------------- | ---- |
| P3.1 | scripts/preprocess_thucnews.py           | ✅ JSONL 格式与标签映射单测 12 用例全过；合成数据 CLI 冒烟通过 |
| P3.2 | scripts/train_classifier.py              | ✅ **真实训练完成（2026-08-26）**：hfl/chinese-roberta-wwm-ext，train 8,560 / val 1,070 / test 1,070（含 military_sina 700 条人工标注），--epochs 2；**test macro F1 = 0.9850 ≥ 0.85 门禁**（val F1: ep1 0.9741 → ep2 0.9784）；产物 data/checkpoints/category/（model.safetensors 409MB）。迷你模型冒烟门禁保留 |
| P3.3 | scripts/export_onnx.py                   | ✅ **真实导出完成**：FP32(opset14)→INT8 动态量化→parity 校验通过；category_classifier_int8.onnx = **98MB ≤ 120MB 门禁**，落位 resources/models/。region_classifier_int8.onnx 仍为占位（region 走域名+地名词典层） |
| P3.4 | core/classifier/pipeline.py + bert_classifier.py + keyword_fallback.py | ✅ §3.2 置信度路由全覆盖，§3.4 八项验收测试齐备（含延迟<25ms / 内存<200MB 门禁，随真实模型自动激活） |
| P3.5 | core/classifier/region_classifier.py     ✅ | 三级策略（域名→地名词典→BERT 辅助）+ 确定性 tie-break 单测覆盖 |

### P3.1 数据契约（P3.2 消费）

- 记录格式（JSONL）：`{"id","title","summary","label","source"}`；
  summary 上限 300 字（SUMMARY_MAX_CHARS），tokenizer 截断在训练/推理侧做。
- 标签映射（LABEL_MAP，10→5；military 无 THUCNews 来源，靠人工标注 --extra 补）：
  时政→politics；财经/股票/房产→economy；科技→tech；
  教育/社会/体育→life；娱乐/游戏→other。
- 均衡采样：THUCNews 各 app 标签上限 = total // 标签数（默认 10000，§3.3）；
  --extra 人工标注整体并入不下采样；80/10/10 按标签分层，固定种子可复现。
- 支持 UTF-8/GBK 双编码；未知类别目录告警跳过；正文 <20 字的退化文档丢弃。

### P3.2–P3.5 关键决策与踩坑记录

1. **训练环境（本仓库 .venv 实测）**：
   `pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu`
   —— **torch 2.13 在此机器 c10.dll 初始化失败**（WinError 1114），
   onnxruntime 1.29 同类问题 → 固定 onnxruntime==1.20.1（满足 §1.2 ≥1.15）。
   pyproject [train] extra 已声明 torch/onnx/scikit-learn。
2. **onnx C++ schema registry 在本机段错误**（get_all_schemas* 与
   infer_shapes_path 均触发 access violation，protobuf 5.x/7.x、干净 PATH
   皆无法规避）。`export_onnx.py` 内置两道子进程探测防护：
   - `import onnx.reference` 崩溃 → 注入惰性桩
     （ORT 量化仅模块顶层引用 ReferenceEvaluator，INT8 分支从不调用）；
   - `infer_shapes_path` 崩溃 → 以 plain-load 替换
     `load_model_with_shape_infer`（补丁直接写入 `quantize_dynamic.__globals__`，
     因为 ORT 存在重复模块实例，改 sys.modules 属性不生效），并以
     `extra_options={"DefaultTensorType": FLOAT}` 补回激活张量类型
     （BERT 图纯 FP32，语义精确）。健康机器上探针通过、零侵入。
3. **transformers 5.x tokenizer 行为变化**：裸 vocab.txt 拒绝转 fast
   （需 sentencepiece/tiktoken）；tokenizer_config.json 缺
   `tokenizer_class` 时推断失败且无 token_type_ids。tests/conftest.py
   用 tokenizers 底层 API 直接产出标准 tokenizer.json（与 HF 发行目录同构），
   训练/推理代码无需任何兼容分支。
4. **torch.onnx.export**：显式 `dynamo=False`（TorchScript tracer 对 HF
   模型最稳），TypeError 时自动回落 dynamo=True（需可选依赖 onnxscript）；
   LogitsWrapper 把 ModelOutput 展平为单 logits 张量再 trace。
5. **训练循环设计**：手写 loop（AdamW + linear warmup/decay + grad clip 1.0）
   规避 Trainer API 版本漂移；best-by-val-F1 权重驻留 CPU 后回载保存；
   标签空间固定 CATEGORIES 六类（数据缺类也保持输出维度一致，便于增量微调）。
6. **P3.4 可测性设计**：BertClassifier 依赖注入（OnnxSession/TokenizerFunc
   两个 Protocol），测试用可编程 FakeSession 精确控制置信度走查全部路由
   分支；重依赖全部延迟导入，core 包 import 不付冷启动代价（§1.3 ≤3s）。
7. **region 三级策略**：域名表 longest-key-wins（防 "reuters japan bureau"
   被 "reuters" 截胡）；地名词典按出现次数计数、并列时按 REGIONS 声明序
   破平（确定性）；BERT 辅助仅在 ≥CONFIDENCE_THRESHOLD_HIGH 且 label∈REGIONS\{unknown}
   时采纳。词表为合理预设，后续可在 settings.yaml 外挂覆盖。

## Phase 4 UI 层 ✅

| ID   | 产出物 | 验收 |
| ---- | ------ | ---- |
| P4.1 | ui/main_window.py（三栏骨架+菜单/状态栏/动作） | ✅ 启动无报错，tests/test_main_window.py；信号槽契约：refresh_requested / settings_requested / settings_applied / category_selected / region_selected / search_submitted / article_selected |
| P4.2 | ui/sidebar.py + attach_sidebar 联动 | ✅ 信号触发+窗口转发，tests/test_sidebar.py |
| P4.3 | ui/news_list.py（QAbstractListModel+QListView） | ✅ Model/View 只绘制可视行，1000 条渲染实测 ≥30fps 门禁入测，tests/test_news_list.py |
| P4.4 | ui/news_detail.py（QTextBrowser） | ✅ HTML 渲染/纯文本转义/来源标注/原文链接可点击，tests/test_news_detail.py |
| P4.5 | ui/search_bar.py（防抖 FTS5） | ✅ 实时过滤+空查询还原列表，FTS5 端到端，tests/test_search_bar.py |
| P4.6 | ui/settings_dialog.py + core/utils/config.py | ✅ 编辑不污染原 dict、accept 后持久化 YAML（原子写、保序、CJK 可读），tests/test_settings_dialog.py |
| P4.7 | ui/themes/light.qss + dark.qss + apply_theme | ✅ 切换即时生效（含 设置→保存→换肤 全链路），tests/test_themes.py |
| P4.8 | main.py + ui/workers.py + core/fetcher/scheduler.py | ✅ 抓取/查询/详情全走 QThread；APScheduler 定时经主线程桥信号防跨线程 UI 访问；bootstrap 组装冒烟，tests/test_workers.py |

### Phase 4 新增文件（§5 目录结构增补）

- `ui/workers.py` — RefreshWorker / QueryWorker(QThread)，DB 连接线程内自建
- `core/utils/config.py` — settings.yaml 读写（P4.6 持久化）
- `core/fetcher/scheduler.py` — 本期实现（原为空壳），APScheduler 封装
- `core/utils/logger.py` — 本期补齐（原为空壳）
- `ui/themes/{light,dark}.qss` — §5 原有规划文件
- `tests/conftest.py` 增加 `qapp` fixture（offscreen 平台）与 Qt 引导调用

### Phase 4 关键决策与踩坑记录

1. **PySide6 锁定 `>=6.5,<6.9`（本机装 6.8.3）**。6.11.2 wheel 在本机
   （Anaconda 底座 + Windows 系统 ICU 更新）导入时按加载路径不同，
   或抛 WinError 127 或直接 fastfail 崩进程（0xC0000409）；降级 6.8.3
   后裸导入即正常。ui/__init__.py 保留探测式 DLL 引导兜底
   （LOAD_WITH_ALTERED_SEARCH_PATH 仅预载 Qt6*.dll；**不预载** VC 运行时，
   避免与 python.exe 已加载副本形成双 CRT 堆错配）。
2. **QTest.keyClicks 输入 CJK 会崩 Windows 进程**（ASCII 正常）。UI 测试
   一律用 setText/QTest.keyClick(单键) 模拟输入。
3. **中文搜索是硬需求但 unicode61 分词器做不到**：连续汉字串是单 token，
   "芯片" 匹配不到 "芯片出口新规"。新增迁移 `002_fts_trigram.sql`
   （DROP+重建 fts5 tokenize='trigram' + rebuild，幂等），对 §2.3 DDL 的
   偏差在此记录。trigram 只支持 ≥3 字符子串 → crud.search_articles 对
   <3 字符 CJK 查询回退 LIKE 扫描（本地库量级毫秒级），MATCH 语法错误
   同样回退，双保险。
4. **mini_checkpoint 未设种子导致 INT8 parity 断言顺序相关闪失败**
   （随机权重下 argmax_agreement 属临界事件）。conftest 中
   torch.manual_seed 固定，P3 测试基建随之稳定。
5. **worker 判重语义**：insert_article 重复 URL 返回旧 id；RefreshWorker
   以 `返回 id == article.id` 判定"新入库"。真实重抓同 URL 的文章会带新
   uuid，测试夹具须模拟该行为（固定 source_url + 递增 id）。
6. **调度器跨线程契约**：APScheduler 回调发生在其守护线程，绝不直接碰
   Qt 对象——回调只 emit 主线程 QObject 桥的信号（queued delivery）。
7. **陈旧回复防护**：侧栏筛选/搜索并发切换时以代际计数器丢弃过期
   QueryWorker 结果，防止列表回跳。
8. **无模型时的分类降级链**（衔接 P3.4/P3.5）：pipeline(classifier=None)
   走关键词层，RegionClassifier() 默认域名+地名词典层；模型工件落位
   resources/models 后自动升级为完整三级策略，无需改代码。
9. **信号纯度**：act_settings 只发 settings_requested；open_settings 接线
   放在 main.py，保证窗口级单测不弹模态框。

## Phase 5 打包发布 ⏳ 未开始

P5.1 GlobalNewsHub.spec（含 RSSHub 二进制 + ONNX 模型）→ P5.2 LICENSE-THIRD-PARTY
→ P5.3 README。

## 常用命令

```
.venv\Scripts\python -m pytest tests/          # 全量测试
.venv\Scripts\python -m mypy --strict core/    # 类型门禁
.venv\Scripts\python -m ruff check core/       # 风格门禁
bash scripts/build_rsshub.sh                   # P2.1：打包 RSSHub（需 Node/git/网络，幂等）

# Phase 3 训练链（需 [train] extra + THUCNews 数据；本机已验证脚本全链路）
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
pip install "onnxruntime==1.20.1" onnx scikit-learn
python scripts/preprocess_thucnews.py --data-dir <THUCNews> --out-dir data/processed
python scripts/train_classifier.py --train data/processed/train.jsonl \
    --val data/processed/val.jsonl --test data/processed/test.jsonl \
    --tokenizer-dir resources/models/tokenizer --out-dir data/checkpoints/category
python scripts/export_onnx.py --checkpoint data/checkpoints/category \
    --tokenizer-dir resources/models/tokenizer --out-dir resources/models \
    --name category_classifier
```

## 备注

- resources/rsshub-server* 为 scripts/build_rsshub.sh 的本地产物，不入库
  （体积各约 100-135MB）；git 中保留占位文件的决策见首次提交。
- RSSHub 构建缓存位于 build/（已被 .gitignore 覆盖）。
- **真实模型已就位（2026-08-26）**：resources/models/tokenizer/ 为
  hfl/chinese-roberta-wwm-ext 官方 tokenizer；category_classifier_int8.onnx
  （98MB）为真实训练产物。RefreshWorker 分类链已自动升级为完整三级策略
  （BERT 模型层激活）。region_classifier_int8.onnx 仍占位，如需 BERT 辅助
  地区判断，可用同 checkpoint 结构以 REGIONS 标签另行训练导出。
- 训练运维记录：首次训练（3 epochs）在 epoch 3 因机器重启中断（脚本仅
  全程结束才落盘，属已知设计）；重跑 --epochs 2 达标。CPU(16 线程)
  实测 ~8s/步（含系统争抢），单 epoch 约 45-65 分钟。
- 本机 .venv 为 Anaconda 底座：torch 固定 2.6.0+cpu、onnxruntime 固定
  1.20.1（更新版本存在 DLL/段错误问题，详见 Phase 3 决策记录第 1、2 条）；
  PySide6 锁定 <6.9（6.8.3 实测可用，见 Phase 4 决策第 1 条）。
- 运行应用：`.venv\Scripts\python main.py`（offscreen 测试环境已验证组装；
  真实窗口/抓取待人类验收）。
