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
| POST | `/api/upload` | 上传PDF，返回task_id |
| GET | `/api/progress/{task_id}` | SSE进度推送 |
| GET | `/api/download/{task_id}/{file_type}` | 下载结果(pdf/markdown) |
| GET | `/api/health` | 服务健康检查 |

## 任务管理
- 内存字典 `tasks: dict[str, dict]` 管理任务状态
- 后台用 `asyncio.create_task` + `pipeline.convert_async` 执行转换
- 进度通过 progress_callback 更新 tasks 字典，SSE 轮询推送
