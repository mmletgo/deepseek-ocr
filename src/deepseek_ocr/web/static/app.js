/**
 * DeepSeek-OCR Web 前端应用
 *
 * 核心功能:
 * 1. 拖拽上传PDF文件
 * 2. 文件选择上传
 * 3. 通过SSE实时接收转换进度
 * 4. 显示下载链接
 * 5. 错误处理和状态管理
 *
 * 纯原生JS实现，不依赖任何框架
 */

(function () {
    "use strict";

    // --- DOM元素引用 ---
    const uploadZone = document.getElementById("uploadZone");
    const fileInput = document.getElementById("fileInput");
    const uploadBtn = document.getElementById("uploadBtn");
    const fileInfo = document.getElementById("fileInfo");
    const fileInfoName = document.getElementById("fileInfoName");
    const fileInfoSize = document.getElementById("fileInfoSize");
    const progressSection = document.getElementById("progressSection");
    const progressBar = document.getElementById("progressBar");
    const progressPercent = document.getElementById("progressPercent");
    const progressStatus = document.getElementById("progressStatus");
    const resultSection = document.getElementById("resultSection");
    const downloadPdfBtn = document.getElementById("downloadPdfBtn");
    const downloadMdBtn = document.getElementById("downloadMdBtn");
    const errorSection = document.getElementById("errorSection");
    const errorText = document.getElementById("errorText");
    const resetBtn = document.getElementById("resetBtn");
    const healthDot = document.getElementById("healthDot");
    const healthText = document.getElementById("healthText");

    // --- 阶段标签映射（phase -> 显示文本）---
    const PHASE_LABELS = {
        queued: "Queued",
        reading: "Reading PDF",
        waiting_ocr: "Waiting for GPU",
        ocr: "Running OCR",
        parsing: "Parsing results",
        waiting_generate: "Waiting to generate",
        generating: "Generating PDF",
        markdown: "Generating Markdown",
        completed: "Completed",
    };

    // --- 状态变量 ---
    let currentTaskId = null;
    let eventSource = null;
    let isUploading = false;

    // --- 初始化 ---
    init();

    function init() {
        // 绑定拖拽事件
        bindDragEvents();
        // 绑定文件选择事件
        bindFileEvents();
        // 绑定重置按钮
        bindResetEvent();
        // 检查服务健康状态
        checkHealth();
    }

    // --- 拖拽上传事件绑定 ---
    function bindDragEvents() {
        // 阻止浏览器默认拖拽行为
        ["dragenter", "dragover", "dragleave", "drop"].forEach(function (eventName) {
            uploadZone.addEventListener(eventName, function (e) {
                e.preventDefault();
                e.stopPropagation();
            });
        });

        // 拖入时高亮
        ["dragenter", "dragover"].forEach(function (eventName) {
            uploadZone.addEventListener(eventName, function () {
                if (!isUploading) {
                    uploadZone.classList.add("drag-over");
                }
            });
        });

        // 拖出时取消高亮
        ["dragleave", "drop"].forEach(function (eventName) {
            uploadZone.addEventListener(eventName, function () {
                uploadZone.classList.remove("drag-over");
            });
        });

        // 拖放文件处理
        uploadZone.addEventListener("drop", function (e) {
            if (isUploading) return;
            var files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0]);
            }
        });
    }

    // --- 文件选择事件绑定 ---
    function bindFileEvents() {
        // 点击上传按钮触发文件选择
        uploadBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            if (!isUploading) {
                fileInput.click();
            }
        });

        // 点击上传区域也触发文件选择
        uploadZone.addEventListener("click", function () {
            if (!isUploading) {
                fileInput.click();
            }
        });

        // 文件选择后处理
        fileInput.addEventListener("change", function () {
            if (fileInput.files.length > 0) {
                handleFile(fileInput.files[0]);
            }
        });
    }

    // --- 重置按钮事件绑定 ---
    function bindResetEvent() {
        resetBtn.addEventListener("click", function () {
            resetUI();
        });
    }

    /**
     * 处理选中的文件
     * 验证文件类型后显示文件信息并开始上传
     */
    function handleFile(file) {
        // 验证文件类型
        if (!file.name.toLowerCase().endsWith(".pdf")) {
            showError("Please select a PDF file.");
            return;
        }

        // 显示文件信息
        fileInfoName.textContent = file.name;
        fileInfoSize.textContent = formatFileSize(file.size);
        fileInfo.classList.add("visible");

        // 开始上传
        uploadFile(file);
    }

    /**
     * 上传文件到服务器
     * 使用fetch POST请求上传PDF文件
     */
    async function uploadFile(file) {
        if (isUploading) return;
        isUploading = true;

        // 禁用上传区域
        uploadZone.classList.add("disabled");
        hideError();
        hideResult();

        // 显示进度区域
        showProgress();
        updateProgress(0, 0, "Uploading file...");

        var formData = new FormData();
        formData.append("file", file);

        try {
            var response = await fetch("/api/upload", {
                method: "POST",
                body: formData,
            });

            if (!response.ok) {
                var errorData = await response.json().catch(function () {
                    return { detail: "Upload failed (HTTP " + response.status + ")" };
                });
                throw new Error(errorData.detail || "Upload failed");
            }

            var data = await response.json();
            currentTaskId = data.task_id;

            updateProgress(0, 0, "Upload complete, starting conversion...");

            // 连接SSE获取进度
            connectSSE(currentTaskId);

        } catch (err) {
            isUploading = false;
            uploadZone.classList.remove("disabled");
            hideProgress();
            showError("Upload failed: " + err.message);
        }
    }

    /**
     * 连接SSE获取转换进度
     * 使用EventSource监听服务器推送的进度事件
     */
    function connectSSE(taskId) {
        // 关闭旧连接
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }

        eventSource = new EventSource("/api/progress/" + taskId);

        // 监听进度事件
        eventSource.addEventListener("progress", function (e) {
            var data = JSON.parse(e.data);
            updateProgress(data.current, data.total, data.status);

            if (data.done) {
                eventSource.close();
                eventSource = null;
                isUploading = false;

                if (data.error) {
                    // 转换出错
                    showError("Conversion failed: " + data.error);
                    resetBtn.classList.add("visible");
                } else {
                    // 转换完成
                    showResult(taskId);
                    resetBtn.classList.add("visible");
                }
            }
        });

        // 错误处理
        eventSource.addEventListener("error", function (e) {
            // SSE连接错误
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            // 检查是否为正常关闭（EventSource会在服务端关闭后尝试重连触发error）
            if (isUploading) {
                isUploading = false;
                showError("Lost connection to server. Please try again.");
                resetBtn.classList.add("visible");
            }
        });
    }

    /**
     * 更新进度条UI
     * 根据当前页数和总页数计算进度百分比
     */
    function updateProgress(current, total, status) {
        var percent = 0;
        if (total > 0) {
            percent = Math.round((current / total) * 100);
        }

        progressBar.style.width = percent + "%";
        progressPercent.textContent = percent + "%";
        progressStatus.textContent = status || "";

        // 完成时移除动画效果
        if (percent >= 100) {
            progressBar.classList.add("completed");
        } else {
            progressBar.classList.remove("completed");
        }
    }

    // --- 显示/隐藏进度区域 ---
    function showProgress() {
        progressSection.classList.add("visible");
        progressBar.style.width = "0%";
        progressBar.classList.remove("completed");
        progressPercent.textContent = "0%";
        progressStatus.textContent = "";
    }

    function hideProgress() {
        progressSection.classList.remove("visible");
    }

    /**
     * 显示转换结果下载区域
     * 设置下载链接的href属性
     */
    function showResult(taskId) {
        downloadPdfBtn.href = "/api/download/" + taskId + "/pdf";
        downloadMdBtn.href = "/api/download/" + taskId + "/markdown";
        resultSection.classList.add("visible");
    }

    function hideResult() {
        resultSection.classList.remove("visible");
    }

    // --- 错误提示 ---
    function showError(message) {
        errorText.textContent = message;
        errorSection.classList.add("visible");
    }

    function hideError() {
        errorSection.classList.remove("visible");
    }

    /**
     * 重置UI到初始状态
     * 清除所有状态并恢复上传区域
     */
    function resetUI() {
        // 关闭SSE连接
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }

        currentTaskId = null;
        isUploading = false;

        // 重置文件输入
        fileInput.value = "";

        // 隐藏所有状态区域
        fileInfo.classList.remove("visible");
        hideProgress();
        hideResult();
        hideError();
        resetBtn.classList.remove("visible");

        // 恢复上传区域
        uploadZone.classList.remove("disabled");
    }

    /**
     * 检查后端服务健康状态
     * 通过/api/health接口获取Ollama连接状态
     */
    async function checkHealth() {
        try {
            var response = await fetch("/api/health");
            var data = await response.json();

            if (data.ollama === "connected" && data.model_available) {
                healthDot.classList.add("ok");
                healthDot.classList.remove("error");
                healthText.textContent = "Service online - Model: " + data.model;
            } else if (data.ollama === "connected") {
                healthDot.classList.add("error");
                healthDot.classList.remove("ok");
                healthText.textContent = "Ollama connected, model not found";
            } else {
                healthDot.classList.add("error");
                healthDot.classList.remove("ok");
                healthText.textContent = "Ollama not available";
            }
        } catch (err) {
            healthDot.classList.add("error");
            healthDot.classList.remove("ok");
            healthText.textContent = "Service unavailable";
        }
    }

    /**
     * 格式化文件大小
     * 将字节数转换为可读的KB/MB/GB格式
     */
    function formatFileSize(bytes) {
        if (bytes === 0) return "0 B";
        var units = ["B", "KB", "MB", "GB"];
        var i = Math.floor(Math.log(bytes) / Math.log(1024));
        if (i >= units.length) i = units.length - 1;
        return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
    }

})();
