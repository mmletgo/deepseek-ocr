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
| POST | `/api/upload` | 上传PDF(支持pdf_mode参数: dual_layer/rewrite)，返回task_id |
| GET | `/api/progress/{task_id}` | SSE进度推送 |
| GET | `/api/download/{task_id}/{file_type}` | 下载结果(pdf/markdown) |
| GET | `/api/health` | 服务健康检查 |

## 任务管理
- 内存字典 `tasks: dict[str, dict]` 管理任务状态（含 phase、pdf_md5、pdf_mode 字段）
- 后台用 `asyncio.create_task` + `_run_conversion` 直接展开各步骤执行
- 进度通过直接更新 tasks 字典，SSE 轮询推送

## 并发控制
- `_ocr_semaphore: asyncio.Semaphore(1)` 串行化GPU OCR，避免显存溢出
- `_generating_semaphore: asyncio.Semaphore(1)` 串行化PDF生成，避免PyMuPDF GIL争用
- phase 状态: `waiting_ocr` → OCR排队；`waiting_generate` → PDF生成排队
- 两个信号量均懒初始化（在事件循环中首次调用时创建）

## OCR 缓存与断点续传
- 上传后计算PDF的MD5哈希作为缓存key
- 缓存路径: `{upload_dir}/ocr_cache/{pdf_md5}/page_NNNN.json`
- 每次转换前检查每页是否已缓存，仅OCR未缓存的页
- 全部命中缓存时直接跳过OCR阶段

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

## SSE 进度推送
- SSE event_data 包含字段: `current`, `total`, `status`, `phase`, `done`, 可选 `error`
- `phase` 字段用于前端相位徽标 (phase badge) 更新，显示当前任务阶段

## 任务 phase 状态流转
`queued` → `reading` → `waiting_ocr` → `ocr` → `parsing` → `waiting_generate` → `generating` → `markdown` → `completed`
