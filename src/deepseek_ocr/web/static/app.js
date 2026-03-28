/**
 * DeepSeek-OCR Web 前端应用
 *
 * 核心功能:
 * 1. 拖拽上传多个PDF文件
 * 2. 文件选择上传（支持多选）
 * 3. 每个任务独立SSE实时接收转换进度
 * 4. 任务卡片显示各文件进度和下载链接
 * 5. 错误处理和任务状态管理
 *
 * 纯原生JS实现，不依赖任何框架
 */

(function () {
    "use strict";

    // --- DOM 元素 ---
    const uploadZone = document.getElementById("uploadZone");
    const fileInput = document.getElementById("fileInput");
    const uploadBtn = document.getElementById("uploadBtn");
    const taskList = document.getElementById("taskList");
    const clearDoneBtn = document.getElementById("clearDoneBtn");
    const healthDot = document.getElementById("healthDot");
    const healthText = document.getElementById("healthText");

    // --- 状态 ---
    // taskRegistry[taskId] = { filename, eventSource, status }
    var taskRegistry = {};
    var isUploading = false;

    // 统一语言列表
    var LANGUAGES = [
        { value: "English", label: "English" },
        { value: "Simplified Chinese", label: "简体中文" },
        { value: "Traditional Chinese", label: "繁體中文" },
        { value: "Japanese", label: "日本語" },
        { value: "Korean", label: "한국어" },
        { value: "German", label: "Deutsch" },
        { value: "French", label: "Français" },
        { value: "Spanish", label: "Español" },
        { value: "Russian", label: "Русский" },
    ];

    // phase 文字映射
    var PHASE_LABELS = {
        waiting_ocr:           "Waiting for GPU",
        reading_pdf:           "Reading",
        reading:               "Reading",
        ocr:                   "OCR",
        parsing:               "Parsing",
        waiting_generate:      "Waiting to generate",
        generating:            "Generating",
        markdown:              "Markdown",
        waiting_translate:     "Waiting to translate",
        translating:           "Translating",
        generating_translated: "Generating translation",
        done:                  "Done",
        completed:             "Done",
    };

    // --- 初始化 ---
    init();

    function init() {
        bindDragEvents();
        bindFileEvents();
        bindClearBtn();
        bindSegmentedControl();
        bindTranslateToggle();
        checkHealth();
    }

    // --- Segmented Control ---
    function bindSegmentedControl() {
        var segments = document.querySelectorAll('.segment');
        var segmentedTrack = document.getElementById('segmentedTrack');
        var pdfModeInput = document.getElementById('pdfModeInput');

        segments.forEach(function(seg, index) {
            seg.addEventListener('click', function(e) {
                e.stopPropagation(); // 防止触发 uploadZone 的 click
                segments.forEach(function(s) { s.classList.remove('active'); });
                seg.classList.add('active');
                pdfModeInput.value = seg.dataset.value;
                if (index === 0) {
                    segmentedTrack.classList.remove('right');
                } else {
                    segmentedTrack.classList.add('right');
                }
            });
        });
    }

    // --- 语言选项动态渲染（互斥逻辑） ---
    function updateLangOptions() {
        var sourceLang = document.getElementById('sourceLang');
        var targetLang = document.getElementById('targetLang');
        var srcVal = sourceLang.value || 'English';
        var tgtVal = targetLang.value || 'Simplified Chinese';

        // 清空并重建 source options（排除 target 选中的语言）
        sourceLang.innerHTML = '';
        LANGUAGES.forEach(function(lang) {
            if (lang.value !== tgtVal) {
                var opt = document.createElement('option');
                opt.value = lang.value;
                opt.textContent = lang.label;
                if (lang.value === srcVal) opt.selected = true;
                sourceLang.appendChild(opt);
            }
        });

        // 清空并重建 target options（排除 source 选中的语言）
        targetLang.innerHTML = '';
        LANGUAGES.forEach(function(lang) {
            if (lang.value !== srcVal) {
                var opt = document.createElement('option');
                opt.value = lang.value;
                opt.textContent = lang.label;
                if (lang.value === tgtVal) opt.selected = true;
                targetLang.appendChild(opt);
            }
        });
    }

    // --- 翻译 Toggle ---
    function bindTranslateToggle() {
        var toggle = document.getElementById('translateToggle');
        var langSelectors = document.getElementById('langSelectors');
        var translateSwitch = document.getElementById('translateSwitch');

        // 初始化语言选项
        updateLangOptions();

        // 绑定互斥逻辑
        var sourceLang = document.getElementById('sourceLang');
        var targetLang = document.getElementById('targetLang');
        if (sourceLang) sourceLang.addEventListener('change', updateLangOptions);
        if (targetLang) targetLang.addEventListener('change', updateLangOptions);

        if (toggle && langSelectors) {
            toggle.addEventListener('change', function() {
                langSelectors.style.display = toggle.checked ? 'flex' : 'none';
            });
        }
        // 阻止 toggle 区域的点击冒泡到 uploadZone
        var translateOptions = document.getElementById('translateOptions');
        if (translateOptions) {
            translateOptions.addEventListener('click', function(e) {
                e.stopPropagation();
            });
        }
    }

    // --- 拖拽事件（与原来完全一致，只改 handleFile → handleFiles） ---
    function bindDragEvents() {
        ["dragenter", "dragover", "dragleave", "drop"].forEach(function (ev) {
            uploadZone.addEventListener(ev, function (e) {
                e.preventDefault();
                e.stopPropagation();
            });
        });
        ["dragenter", "dragover"].forEach(function (ev) {
            uploadZone.addEventListener(ev, function () {
                if (!isUploading) uploadZone.classList.add("drag-over");
            });
        });
        ["dragleave", "drop"].forEach(function (ev) {
            uploadZone.addEventListener(ev, function () {
                uploadZone.classList.remove("drag-over");
            });
        });
        uploadZone.addEventListener("drop", function (e) {
            if (isUploading) return;
            if (e.dataTransfer.files.length > 0) handleFiles(e.dataTransfer.files);
        });
    }

    function bindFileEvents() {
        uploadBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            if (!isUploading) fileInput.click();
        });
        uploadZone.addEventListener("click", function () {
            if (!isUploading) fileInput.click();
        });
        fileInput.addEventListener("change", function () {
            if (fileInput.files.length > 0) handleFiles(fileInput.files);
        });
    }

    function bindClearBtn() {
        clearDoneBtn.addEventListener("click", function () {
            Object.keys(taskRegistry).forEach(function (taskId) {
                var t = taskRegistry[taskId];
                if (t.status === "done" || t.status === "error") {
                    var el = document.getElementById("task-" + taskId);
                    if (el) el.remove();
                    delete taskRegistry[taskId];
                }
            });
            updateClearBtnVisibility();
        });
    }

    // --- 处理多文件 ---
    async function handleFiles(files) {
        var pdfFiles = Array.from(files).filter(function (f) {
            return f.name.toLowerCase().endsWith(".pdf");
        });
        if (pdfFiles.length === 0) {
            // 没有有效 PDF，简单忽略
            return;
        }

        isUploading = true;
        uploadZone.classList.add("disabled");

        var formData = new FormData();
        pdfFiles.forEach(function (f) {
            formData.append("files", f);   // 字段名 "files"（复数）
        });
        var pdfModeInput = document.getElementById('pdfModeInput');
        formData.append("pdf_mode", pdfModeInput ? pdfModeInput.value : "dual_layer");
        var translateToggle = document.getElementById('translateToggle');
        formData.append("translate", translateToggle && translateToggle.checked ? "true" : "false");
        formData.append("source_lang", document.getElementById('sourceLang') ? document.getElementById('sourceLang').value : "English");
        formData.append("target_lang", document.getElementById('targetLang') ? document.getElementById('targetLang').value : "Simplified Chinese");

        try {
            var response = await fetch("/api/upload", {
                method: "POST",
                body: formData,
            });
            if (!response.ok) {
                var errData = await response.json().catch(function () {
                    return { detail: "Upload failed (HTTP " + response.status + ")" };
                });
                throw new Error(errData.detail || "Upload failed");
            }
            var results = await response.json();  // [{task_id, filename}, ...]
            results.forEach(function (item) {
                createTaskCard(item.task_id, item.filename);
                connectTaskSSE(item.task_id);
            });
        } catch (err) {
            // 显示全局错误（复用 uploadZone 区域提示）
            console.error("Upload error:", err);
            // 简单 alert，不影响已有任务
            alert("Upload failed: " + err.message);
        } finally {
            isUploading = false;
            uploadZone.classList.remove("disabled");
            fileInput.value = "";
        }
    }

    // --- 任务卡片 ---
    function createTaskCard(taskId, filename) {
        taskRegistry[taskId] = { filename: filename, eventSource: null, status: "pending" };

        var downloadSvg = '<svg class="download-btn-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

        var card = document.createElement("div");
        card.className = "task-card";
        card.id = "task-" + taskId;
        card.innerHTML =
            '<div class="task-header">' +
                '<span class="task-filename" title="' + escapeHtml(filename) + '">' + escapeHtml(filename) + '</span>' +
                '<span class="task-phase-badge phase-waiting_ocr" id="badge-' + taskId + '">' + PHASE_LABELS.waiting_ocr + '</span>' +
            '</div>' +
            '<div class="task-progress">' +
                '<div class="progress-bar-container">' +
                    '<div class="progress-bar" id="bar-' + taskId + '" style="width:0%"></div>' +
                '</div>' +
                '<div class="task-status-row">' +
                    '<span class="task-status-text" id="status-' + taskId + '">Waiting...</span>' +
                    '<span class="task-percent" id="pct-' + taskId + '">0%</span>' +
                '</div>' +
            '</div>' +
            '<div class="task-downloads" id="dl-' + taskId + '" style="display:none">' +
                '<a href="/api/download/' + taskId + '/pdf" class="download-btn" download>' +
                    downloadSvg + 'Download PDF' +
                '</a>' +
                '<a href="/api/download/' + taskId + '/markdown" class="download-btn" download>' +
                    downloadSvg + 'Download Markdown' +
                '</a>' +
                '<a href="/api/download/' + taskId + '/translated_pdf" class="download-btn download-translate" download style="display:none">' +
                    downloadSvg + 'Translated PDF' +
                '</a>' +
                '<a href="/api/download/' + taskId + '/bilingual_pdf" class="download-btn download-translate" download style="display:none">' +
                    downloadSvg + 'Bilingual PDF' +
                '</a>' +
            '</div>' +
            '<div class="task-error" id="err-' + taskId + '" style="display:none"></div>';

        taskList.appendChild(card);
    }

    // --- SSE 连接（每任务独立） ---
    function connectTaskSSE(taskId) {
        var es = new EventSource("/api/progress/" + taskId);
        taskRegistry[taskId].eventSource = es;

        es.addEventListener("progress", function (e) {
            var data = JSON.parse(e.data);
            updateTaskCard(taskId, data);

            if (data.done) {
                es.close();
                taskRegistry[taskId].status = data.error ? "error" : "done";
                updateClearBtnVisibility();
            }
        });

        es.addEventListener("error", function () {
            if (taskRegistry[taskId] && taskRegistry[taskId].status === "pending") {
                es.close();
                markTaskError(taskId, "Connection lost");
            }
        });
    }

    // --- 更新卡片 ---
    function updateTaskCard(taskId, data) {
        var total = data.total || 0;
        var current = data.current || 0;
        var pct = total > 0 ? Math.round((current / total) * 100) : 0;

        var bar = document.getElementById("bar-" + taskId);
        var pctEl = document.getElementById("pct-" + taskId);
        var statusEl = document.getElementById("status-" + taskId);
        var badge = document.getElementById("badge-" + taskId);
        var dlEl = document.getElementById("dl-" + taskId);
        var errEl = document.getElementById("err-" + taskId);

        if (bar) {
            bar.style.width = pct + "%";
            if (pct >= 100) bar.classList.add("completed");
            else bar.classList.remove("completed");
        }
        if (pctEl) pctEl.textContent = pct + "%";
        if (statusEl) statusEl.textContent = data.status || "";

        // 更新 phase 徽标
        if (badge && data.phase) {
            badge.className = "task-phase-badge phase-" + data.phase;
            badge.textContent = PHASE_LABELS[data.phase] || data.phase;
        }

        // 完成时显示下载
        if (data.done && !data.error) {
            if (dlEl) dlEl.style.display = "flex";
            // 如果有翻译结果，显示翻译下载按钮
            if (data.has_translation) {
                var translateBtns = document.querySelectorAll('#task-' + taskId + ' .download-translate');
                translateBtns.forEach(function(btn) { btn.style.display = 'inline-flex'; });
            }
        }

        // 出错时显示错误
        if (data.error) {
            if (errEl) {
                errEl.textContent = data.error;
                errEl.style.display = "block";
            }
        }
    }

    function markTaskError(taskId, msg) {
        taskRegistry[taskId].status = "error";
        var errEl = document.getElementById("err-" + taskId);
        if (errEl) {
            errEl.textContent = msg;
            errEl.style.display = "block";
        }
        updateClearBtnVisibility();
    }

    function updateClearBtnVisibility() {
        var hasDone = Object.keys(taskRegistry).some(function (id) {
            var s = taskRegistry[id].status;
            return s === "done" || s === "error";
        });
        clearDoneBtn.style.display = hasDone ? "block" : "none";
    }

    function escapeHtml(text) {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    // --- 健康检查（与原来完全一致） ---
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

    // --- 工具函数（原有，保留） ---
    function formatFileSize(bytes) {
        if (bytes === 0) return "0 B";
        var units = ["B", "KB", "MB", "GB"];
        var i = Math.floor(Math.log(bytes) / Math.log(1024));
        if (i >= units.length) i = units.length - 1;
        return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
    }

})();
