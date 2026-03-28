# web/ - Web界面

## 文件

| 文件 | 职责 |
|------|------|
| `app.py` | FastAPI应用工厂 (create_app) |
| `routes.py` | API路由: 上传/进度SSE/下载/健康检查 |
| `static/index.html` | 单页面前端 |
| `static/style.css` | 样式表 |
| `static/app.js` | 前端逻辑 (拖拽上传/SSE进度/下载) |

## API端点

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 返回主页面 |
| POST | `/api/upload` | 上传PDF(支持pdf_mode/translate/source_lang/target_lang参数)，返回task_id |
| GET | `/api/progress/{task_id}` | SSE进度推送(含has_translation字段) |
| GET | `/api/download/{task_id}/{file_type}` | 下载结果(pdf/markdown/translated_pdf/bilingual_pdf) |
| GET | `/api/health` | 服务健康检查 |

## 任务管理
- 内存字典 `tasks: dict[str, dict]` 管理任务状态（含 phase、pdf_md5、pdf_mode、translate、source_lang、target_lang、result_translated_pdf、result_bilingual_pdf 字段）
- 后台用 `asyncio.create_task` + `_run_conversion` 直接展开各步骤执行
- 进度通过直接更新 tasks 字典，SSE 轮询推送

## 并发控制
- `_ocr_semaphore: asyncio.Semaphore(1)` 串行化GPU OCR，避免显存溢出
- `_generating_semaphore: asyncio.Semaphore(1)` 串行化PDF生成，避免PyMuPDF GIL争用
- `_translation_semaphore: asyncio.Semaphore(2)` 限制翻译并发（最多2个翻译任务同时运行）
- phase 状态: `waiting_ocr` → OCR排队；`waiting_generate` → PDF生成排队；`waiting_translate` → 翻译排队
- 三个信号量均懒初始化（在事件循环中首次调用时创建）

## OCR 缓存与断点续传
- 上传后计算PDF的MD5哈希作为缓存key
- 缓存路径: `{upload_dir}/ocr_cache/{pdf_md5}/page_NNNN.json`
- 每次转换前检查每页是否已缓存，仅OCR未缓存的页
- 全部命中缓存时直接跳过OCR阶段

## 翻译缓存与断点续传
- 缓存路径: `{upload_dir}/translation_cache/{pdf_md5}/{src_lang}_{tgt_lang}/page_NNNN.json`
- 翻译前逐页检查缓存，缓存命中直接加载 TranslatedPage，未命中的页加入待翻译列表
- 全部命中缓存时跳过翻译 API 调用

## 翻译并行
- 未缓存的页通过 `asyncio.gather` + `asyncio.Semaphore(3)` 实现最多3页并行翻译
- 每页翻译完成后立即写入缓存（支持断点续传）
- 进度计数器 `translated_count` 在每页完成后递增并更新任务状态

## 前端设计风格
- **Apple 设计语言**: 温灰背景 (#f5f5f7)、多层阴影 (box-shadow)、大圆角 (16px+)、pill 形按钮
- **暗色模式支持**: 通过 `prefers-color-scheme: dark` 媒体查询自动切换暗色配色
- **SVG 图标**: 使用内联 SVG 图标替代 emoji，保持视觉一致性
- **任务卡片入场动画**: 新任务卡片以 fadeInUp 动画进入
- **健康指示灯**: 脉冲动画 (pulse) 指示服务连接状态

## 前端模式选择
- 上传区域包含 Apple 风格 segmented control：Dual Layer / Rewrite
- FormData 附带 `pdf_mode` 字段传递给后端
- 后端读取 task["pdf_mode"] 传递给 `create_dual_layer_pdf(mode=pdf_mode)`

## 前端翻译选项
- iOS 风格 toggle 开关 "Translate after OCR"，点击后展开语言选择器
- From/To 共享统一语言列表（JS 中 `LANGUAGES` 数组）：English/简体中文/繁體中文/日本語/한국어/Deutsch/Français/Español/Русский
- 互斥逻辑：From 选中的语言不出现在 To 列表中，反之亦然（`updateLangOptions()` 动态渲染）
- 默认值：From=English, To=Simplified Chinese
- HTML 中 select 为空壳，选项由 app.js 在 `bindTranslateToggle()` 初始化时动态填充
- FormData 附带 `translate`、`source_lang`、`target_lang` 字段
- 翻译完成后显示额外下载按钮：Translated PDF 和 Bilingual PDF
- 翻译区域 click 事件阻止冒泡，避免触发 uploadZone 的文件选择

## SSE 进度推送
- SSE event_data 包含字段: `current`, `total`, `status`, `phase`, `done`, `has_translation`, 可选 `error`
- `phase` 字段用于前端相位徽标 (phase badge) 更新，显示当前任务阶段
- `has_translation` 字段用于前端控制翻译下载按钮的显示

## 任务 phase 状态流转
`queued` → `reading` → `waiting_ocr` → `ocr` → `parsing` → `waiting_generate` → `generating` → `markdown` → [`waiting_translate` → `translating` → `generating_translated`] → `completed`

注：方括号内为可选的翻译阶段，仅在用户启用翻译且配置了 API key 时执行
