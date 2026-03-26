# DeepSeek-OCR 产品需求文档

## 产品概述
一个本地运行的OCR工具，使用DeepSeek-OCR模型将英文扫描PDF转化为可搜索的双层PDF和Markdown文件。

## 目标用户
需要将扫描版PDF转换为可搜索、可编辑格式的用户。

## 功能需求

### F1: PDF转换
- 输入: 英文扫描PDF文件
- 输出:
  - 双层PDF: 底层保留原始扫描图像，上层叠加透明可搜索文字层
  - Markdown文件: 结构化的文本内容
- 支持单文件和目录批量处理

### F2: 命令行界面
- `deepseek-ocr convert`: 执行转换，支持DPI/输出目录/模型等参数
- `deepseek-ocr check`: 环境检查（Ollama服务、模型状态）
- `deepseek-ocr serve`: 启动Web服务
- Rich进度条和摘要表格

### F3: Web界面
- 拖拽/点击上传PDF文件
- SSE实时进度推送
- 转换完成后下载PDF和Markdown
- 服务健康状态指示

### F4: 模型推理
- 通过Ollama运行deepseek-ocr模型
- Apple Silicon Metal加速
- 支持grounding模式获取文字坐标
- 3次重试+指数退避容错

## 非功能需求
- macOS Apple Silicon原生支持
- 大PDF逐页处理，控制内存
- OCR失败单页可跳过，不影响整体
- 无坐标标签时自动降级为纯文本模式

## 技术约束
- Python 3.12+
- 依赖Ollama服务运行
- 需要预先拉取deepseek-ocr模型（约6.7GB）
