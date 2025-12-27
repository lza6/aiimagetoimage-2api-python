# -*- coding: utf-8 -*-
import os
import json
import time
import uuid
import threading
import random
import logging
import base64
import webview  # 核心：原生窗口容器
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import requests

# =================================================================
# 核心配置与统计管理 (Configuration & Stats)
# =================================================================
CONFIG = {
    "PROJECT_NAME": "AI 图像驾驶舱 终极版",
    "VERSION": "5.0.0",
    "PORT": 5896,
    "API_KEY": "1",
    "UPSTREAM": "https://api.aiimagetoimage.io",
    "UPSTREAM_BACKUP": [],  # 备用API地址列表，如 ["https://backup-api.example.com"]
    "GA_URL": "https://region1.google-analytics.com/g/collect",
    "DATA_FILE": "cockpit_pro_data.json",
    "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    # API类型配置: "default", "cherry", "openai", "lmstudio", "ollama"
    "API_TYPE": "default",
    # Cherry Studio等本地API地址
    "CHERRY_STUDIO_URL": "http://127.0.0.1:8080",
    "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
    # 模型映射：前端显示名称 -> 实际API模型标识
    "MODEL_MAPPING": {
        "nano_banana": "nano_banana",
        "standard": "standard"
    },
    # 可用模型列表（用于/v1/models端点）
    "MODELS": [
        {"id": "nano_banana", "name": "Nano Banana (极速)", "supports_images": True},
        {"id": "standard", "name": "Standard (高清)", "supports_images": True}
    ]
}

app = Flask(__name__)
CORS(app)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# 数据持久化逻辑
def get_default_data():
    return {
        "stats": {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "last_call_time": "无记录"
        },
        "history": [],
        "settings": {"theme": "obsidian"}
    }

def load_data():
    if os.path.exists(CONFIG["DATA_FILE"]):
        try:
            with open(CONFIG["DATA_FILE"], "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return get_default_data()
    return get_default_data()

def save_data(data):
    with open(CONFIG["DATA_FILE"], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# =================================================================
# 核心引擎 (Engine)
# =================================================================
class ImageEngine:
    @staticmethod
    def get_headers():
        return {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "origin": "https://aiimagetoimage.io",
            "referer": "https://aiimagetoimage.io/",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": CONFIG["USER_AGENT"]
        }

    @staticmethod
    def simulate_ga():
        cid = f"{random.randint(1000000000, 9999999999)}.{int(time.time())}"
        params = {"v": "2", "tid": "G-QN0ECG686N", "cid": cid, "en": "page_view", "dl": "https://aiimagetoimage.io/"}
        try: requests.post(CONFIG["GA_URL"], params=params, timeout=5)
        except: pass

    @staticmethod
    def get_api_url(model_id):
        """根据模型ID和API类型返回对应的API地址"""
        api_type = CONFIG.get("API_TYPE", "default")
        
        # 获取实际模型标识（通过映射）
        actual_model = CONFIG["MODEL_MAPPING"].get(model_id, model_id)
        
        if api_type == "cherry":
            # Cherry Studio API (假设兼容OpenAI格式)
            return f"{CONFIG['CHERRY_STUDIO_URL']}/v1/chat/completions"
        elif api_type == "openai":
            # OpenAI兼容API (LM Studio, Ollama等)
            return f"{CONFIG['OPENAI_BASE_URL']}/chat/completions"
        else:
            # 默认API
            return f"{CONFIG['UPSTREAM']}/api/img2img/image-generate/image2image"

    @staticmethod
    def get_all_api_urls(model_id):
        """获取所有可用的API地址（主API + 备用API）"""
        api_type = CONFIG.get("API_TYPE", "default")
        
        if api_type == "default":
            urls = [f"{CONFIG['UPSTREAM']}/api/img2img/image-generate/image2image"]
            # 添加备用API地址
            for backup in CONFIG.get("UPSTREAM_BACKUP", []):
                urls.append(f"{backup}/api/img2img/image-generate/image2image")
            return urls
        else:
            # 其他API类型返回单个地址
            return [ImageEngine.get_api_url(model_id)]

    @staticmethod
    def prepare_request_data(api_type, model_id, prompt, image_data=None, aspect_ratio="match_input_image"):
        """根据不同API类型准备请求数据"""
        actual_model = CONFIG["MODEL_MAPPING"].get(model_id, model_id)
        
        if api_type in ["cherry", "openai"]:
            # OpenAI兼容格式
            messages = [{"role": "user", "content": []}]
            if prompt:
                messages[0]["content"].append({"type": "text", "text": prompt})
            if image_data and "base64," in image_data:
                messages[0]["content"].append({
                    "type": "image_url",
                    "image_url": {"url": image_data}
                })
            
            return {
                "model": actual_model,
                "messages": messages,
                "stream": True
            }
        else:
            # 默认API格式
            return {
                "prompt": prompt,
                "negative_prompt": "",
                "model_type": actual_model,
                "aspect_ratio": aspect_ratio
            }

    @staticmethod
    def get_api_headers(api_type):
        """根据API类型获取请求头"""
        if api_type in ["cherry", "openai"]:
            # OpenAI兼容API头
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CONFIG['API_KEY']}"
            }
        else:
            # 默认API头
            return ImageEngine.get_headers()

    @staticmethod
    def process_api_response(api_type, response):
        """处理不同API类型的响应"""
        if api_type in ["cherry", "openai"]:
            # OpenAI兼容API响应
            return response.json()
        else:
            # 默认API响应
            return response.json()

# =================================================================
# API 路由 (Routes)
# =================================================================

@app.route('/api/data', methods=['GET'])
def get_all_data():
    return jsonify(load_data())

@app.route('/api/theme', methods=['POST'])
def set_theme():
    theme = request.json.get("theme")
    data = load_data()
    data["settings"]["theme"] = theme
    save_data(data)
    return jsonify({"status": "success"})

@app.route('/v1/models', methods=['GET'])
def list_models():
    """返回OpenAI兼容的模型列表，包含显示名称和图像支持信息"""
    models = []
    for model in CONFIG["MODELS"]:
        models.append({
            "id": model["id"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": "system",
            "permission": [],
            "root": model["id"],
            "parent": None,
            # 扩展字段，用于前端显示
            "display_name": model.get("name", model["id"]),
            "supports_images": model.get("supports_images", True)
        })
    return jsonify({
        "object": "list",
        "data": models
    })

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    body = request.json
    messages = body.get("messages", [])
    last_msg = messages[-1]["content"]
    
    prompt = ""
    image_data = None

    if isinstance(last_msg, list):
        for part in last_msg:
            if part["type"] == "text": prompt = part["text"]
            if part["type"] == "image_url": image_data = part["image_url"]["url"]
    else:
        prompt = last_msg

    def generate():
        # 辅助函数：生成符合OpenAI规范的调试信息Chunk
        def debug_chunk(msg):
             chunk = {
                "id": f"chatcmpl-{uuid.uuid4()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "nano_banana"),
                "choices": [{
                    "index": 0, 
                    # 将调试日志做为内容输出，或者使用特殊的注释格式让前端处理
                    # 这里为了兼容性，我们直接输出为文本，但加上特定的前缀
                    "delta": {"content": f"\n`{msg}`\n"}, 
                    "finish_reason": None
                }]
            }
             return f"data: {json.dumps(chunk)}\n\n"

        yield debug_chunk(">>> [系统] 正在初始化原生渲染引擎...")
        ImageEngine.simulate_ga()
        
        files = {}
        if image_data and "base64," in image_data:
            try:
                header, encoded = image_data.split(",", 1)
                img_bytes = base64.b64decode(encoded)
                files['image'] = ('product.jpg', img_bytes, 'image/jpeg')
            except:
                yield debug_chunk(">>> [错误] 图像解码失败")
        else:
             # 如果没有提供图片，使用默认的1x1黑色像素图片以满足API必须有图片的要求
             try:
                 # 1x1 黑色 JPEG 像素
                 pixel_b64 = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
                 img_bytes = base64.b64decode(pixel_b64)
                 files['image'] = ('pixel.jpg', img_bytes, 'image/jpeg')
                 yield debug_chunk(">>> [提示] 未提供参考图，已自动填充空白底图")
             except:
                 pass

        # 准备请求数据和URL
        
        # 准备请求数据（使用上游API格式）
        data = {
            "prompt": prompt,
            "negative_prompt": "",
            "model_type": body.get("model", "nano_banana"),
            "aspect_ratio": body.get("aspect_ratio", "match_input_image")
        }
        
        # 伪造IP (Soft IP Spoofing)
        def get_random_ip():
            return f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(0,255)}"
            
        spoofed_headers = ImageEngine.get_headers()
        fake_ip = get_random_ip()
        spoofed_headers["X-Forwarded-For"] = fake_ip
        spoofed_headers["X-Real-IP"] = fake_ip
        
        # 代理配置
        # 【重要】如果上游封锁了IP，必须使用真实代理（梯子）。
        # 这里默认尝试连接常见的本地代理端口 7890 (Clash/v2ray等)。
        # 如果你的代理端口不同（如 10809），请修改下面的端口号。
        # 如果没有代理，请将 proxies 设置为 None
        proxies = {
            "http": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890"
        }
        
        try:
            yield debug_chunk(">>> [网络] 正在通过代理隧道连接上游 (Proxy: 127.0.0.1:7890)...")
            
            # 直接请求上游API
            resp = requests.post(
                f"{CONFIG['UPSTREAM']}/api/img2img/image-generate/image2image",
                headers=spoofed_headers,
                data=data,
                files=files,
                timeout=30,
                proxies=proxies
            )
            res_json = resp.json()
            
            if res_json.get("code") == 200:
                job_id = res_json["result"]["job_id"]
                yield debug_chunk(f">>> [成功] 任务已进入渲染队列: {job_id}")
                
                # 轮询结果
                start_time = time.time()
                while time.time() - start_time < 300:
                    poll = requests.get(
                        f"{CONFIG['UPSTREAM']}/api/result/get",
                        params={"job_id": job_id},
                        headers=spoofed_headers, # 保持一致的Header
                        proxies=proxies,
                        timeout=10
                    )
                    p_data = poll.json()
                    
                    if p_data.get("code") == 200 and p_data.get("result", {}).get("image_url"):
                        url = p_data["result"]["image_url"][0]
                        
                        # 更新统计与历史
                        full_data = load_data()
                        full_data["stats"]["total_calls"] += 1
                        full_data["stats"]["success_calls"] += 1
                        full_data["stats"]["last_call_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        full_data["history"].insert(0, {"prompt": prompt, "url": url, "time": datetime.now().strftime("%H:%M:%S")})
                        full_data["history"] = full_data["history"][:50]
                        save_data(full_data)

                        # 返回OpenAI兼容格式
                        chunk = {
                            "id": f"chatcmpl-{uuid.uuid4()}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": body.get("model", "nano_banana"),
                            "choices": [{"index": 0, "delta": {"content": f"\n\n![Result]({url})"}, "finish_reason": "stop"}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        break
                    elif p_data.get("code") == 202:
                        pass
                    time.sleep(3)
                else:
                    # 超时
                    yield debug_chunk(">>> [错误] 渲染超时，请重试")
            else:
                # 尝试提取具体错误信息
                err_msg = "上游服务器返回错误"
                if res_json.get("message"):
                    if isinstance(res_json["message"], dict):
                        err_msg = res_json["message"].get("zh", res_json["message"].get("en", str(res_json["message"])))
                    else:
                        err_msg = str(res_json["message"])
                
                # 记录失败统计
                full_data = load_data()
                full_data["stats"]["total_calls"] += 1
                full_data["stats"]["failed_calls"] += 1
                save_data(full_data)
                
                yield debug_chunk(f">>> [拒绝] {err_msg} (Code: {res_json.get('code')})")

        except Exception as e:
            yield debug_chunk(f">>> [异常] {str(e)}")
        
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# =================================================================
# 终极原生感 UI (HTML/CSS/JS)
# =================================================================
@app.route('/')
def index():
    html_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{{PROJECT_NAME}}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;900&family=Noto+Sans+SC:wght@300;400;700&display=swap');
        
        /* 主题变量定义 */
        :root {
            --bg: #0F0F12;
            --sidebar: #16161D;
            --card: #1C1C26;
            --border: rgba(255, 255, 255, 0.08);
            --primary: #FFBF00;
            --primary-glow: rgba(255, 191, 0, 0.3);
            --text: #FFFFFF;
            --text-dim: #8E8E93;
            --titlebar: #0F0F12;
        }

        [data-theme="deepsea"] {
            --bg: #050B14; --sidebar: #0A1628; --card: #0F2038; --primary: #007AFF; --primary-glow: rgba(0, 122, 255, 0.3);
        }

        [data-theme="cyber"] {
            --bg: #0D0216; --sidebar: #1A042D; --card: #260642; --primary: #BF00FF; --primary-glow: rgba(191, 0, 255, 0.3);
        }

        body {
            margin: 0; padding: 0; background: var(--bg); color: var(--text);
            font-family: 'Inter', 'Noto Sans SC', sans-serif; height: 100vh;
            display: flex; flex-direction: column; overflow-y: auto; /* 启用垂直滚动 */
            user-select: none;
            border: 1px solid var(--border); /* 窗口边框，便于拖拽调整大小 */
            box-sizing: border-box;
        }

        /* 原生感标题栏 */
        .title-bar {
            height: 38px; background: var(--titlebar); display: flex;
            justify-content: space-between; align-items: center;
            padding: 0 15px; -webkit-app-region: drag; /* 允许拖拽窗口 */
            border-bottom: 1px solid var(--border); z-index: 9999;
        }

        .title-bar .app-info { display: flex; align-items: center; gap: 10px; font-size: 12px; font-weight: 600; color: var(--text-dim); }
        .title-bar .controls { display: flex; gap: 5px; -webkit-app-region: no-drag; }
        .control-btn {
            width: 32px; height: 24px; display: flex; align-items: center; justify-content: center;
            border-radius: 4px; cursor: pointer; transition: 0.2s;
        }
        .control-btn:hover { background: rgba(255,255,255,0.1); }
        .control-btn.close:hover { background: #FF3B30; }

        /* 布局架构 */
        .app-container { flex: 1; display: flex; overflow: auto; }

        .sidebar {
            width: 360px; background: var(--sidebar); border-right: 1px solid var(--border);
            display: flex; flex-direction: column; padding: 20px; box-sizing: border-box;
            overflow-y: auto; /* 侧边栏垂直滚动 */
        }

        .main-view { flex: 1; display: flex; flex-direction: column; background: var(--bg); padding: 20px; position: relative; overflow-y: auto; min-height: 0; }

        /* 高级卡片 */
        .card {
            background: var(--card); border: 1px solid var(--border);
            border-radius: 14px; padding: 16px; margin-bottom: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
        }

        .label { font-size: 10px; font-weight: 800; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; display: block; }

        /* 可复制的输入框 */
        .copy-box {
            background: #000; border: 1px solid #333; border-radius: 8px;
            padding: 10px; display: flex; justify-content: space-between; align-items: center;
            font-family: 'Fira Code', monospace; font-size: 11px; color: var(--primary); margin-bottom: 8px;
        }
        .copy-btn { cursor: pointer; opacity: 0.6; transition: 0.2s; padding: 4px; }
        .copy-btn:hover { opacity: 1; color: #fff; }

        /* 统计网格 */
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .stat-card { background: rgba(0,0,0,0.2); padding: 12px; border-radius: 10px; border: 1px solid var(--border); }
        .stat-val { font-size: 20px; font-weight: 900; color: var(--primary); }
        .stat-lbl { font-size: 10px; color: var(--text-dim); margin-top: 4px; }

        /* 交互组件 */
        select, textarea {
            width: 100%; background: rgba(0,0,0,0.3); border: 1px solid #333; color: #fff;
            padding: 12px; border-radius: 10px; font-family: inherit; margin-bottom: 12px; outline: none; transition: 0.3s;
        }
        select:focus, textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }

        .upload-zone {
            border: 2px dashed #444; border-radius: 12px; padding: 25px 10px; text-align: center;
            cursor: pointer; transition: 0.3s; background: rgba(255,255,255,0.02); margin-bottom: 12px;
        }
        .upload-zone:hover { border-color: var(--primary); background: rgba(255, 191, 0, 0.05); }
        #preview-img { max-width: 100%; max-height: 150px; border-radius: 8px; display: none; margin: 0 auto; }

        .btn-action {
            width: 100%; padding: 14px; background: var(--primary); color: #000; border: none;
            border-radius: 12px; font-weight: 900; font-size: 14px; cursor: pointer; transition: 0.3s;
        }
        .btn-action:hover { transform: translateY(-2px); box-shadow: 0 8px 20px var(--primary-glow); }

        /* 终端与画廊 */
        .terminal-container { flex: 1; display: flex; flex-direction: column; background: #050505; border: 1px solid var(--border); border-radius: 16px; overflow: hidden; }
        .terminal-header { background: #111; padding: 10px 20px; display: flex; justify-content: space-between; font-size: 11px; color: var(--text-dim); }
        .terminal-body { flex: 1; padding: 15px; overflow-y: auto; font-family: 'Fira Code', monospace; font-size: 12px; line-height: 1.6; }
        
        .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap: 8px; }
        .gallery-item { aspect-ratio: 1; border-radius: 8px; overflow: hidden; border: 1px solid #333; cursor: pointer; transition: 0.2s; }
        .gallery-item:hover { border-color: var(--primary); transform: scale(1.05); }
        .gallery-item img { width: 100%; height: 100%; object-fit: cover; }

        .theme-selector { display: flex; gap: 8px; margin-top: 10px; }
        .theme-dot { width: 16px; height: 16px; border-radius: 50%; cursor: pointer; border: 2px solid transparent; }
        .theme-dot.active { border-color: #fff; }

        /* Cherry Studio 风格日志卡片 */
        .log-card {
            background-color: #FFF0F0;
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 8px;
            border: 1px solid #E0C0C0;
            font-family: 'Consolas', monospace;
            font-size: 14px;
            line-height: 1.6;
            position: relative;
        }
        .log-card.debug {
            background-color: #F0F8FF;
            border-color: #C0D0E0;
        }
        .log-card.error {
            background-color: #FFF0F0;
            border-color: #E0C0C0;
        }
        .log-card .log-content {
            margin-right: 80px;
            word-break: break-all;
            white-space: pre-wrap;
        }
        .log-card .log-actions {
            position: absolute;
            right: 12px;
            top: 12px;
            display: flex;
            gap: 8px;
        }
        .log-card .log-detail {
            color: #0066CC;
            cursor: pointer;
            font-size: 12px;
            text-decoration: none;
        }
        .log-card .log-close {
            color: #999;
            cursor: pointer;
            font-size: 16px;
            line-height: 1;
        }
        .log-card .log-close:hover {
            color: #D00;
        }

        /* 成功提示 */
        #toast {
            position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            background: var(--primary); color: #000; padding: 8px 20px; border-radius: 20px;
            font-size: 12px; font-weight: 700; display: none; z-index: 10000;
        }
    </style>
</head>
<body data-theme="obsidian">
    <!-- 原生标题栏 -->
    <div class="title-bar">
        <div class="app-info">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="var(--primary)"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"></path></svg>
            {{PROJECT_NAME}} v{{VERSION}}
        </div>
        <div class="controls">
            <div class="control-btn" onclick="window.pywebview.api.minimize()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"></line></svg>
            </div>
            <div class="control-btn" onclick="window.pywebview.api.toggle_maximize()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"/></svg>
            </div>
            <div class="control-btn close" onclick="window.pywebview.api.close()">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </div>
        </div>
    </div>

    <div class="app-container">
        <!-- 侧边栏 -->
        <div class="sidebar">
            <div class="card">
                <span class="label">数据看板</span>
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-val" id="stat-total">0</div>
                        <div class="stat-lbl">累计请求</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-val" id="stat-success">0</div>
                        <div class="stat-lbl">成功渲染</div>
                    </div>
                </div>
                <div style="margin-top:12px; font-size:10px; color:var(--text-dim);">最近活动: <span id="stat-last" style="color:#fff;">-</span></div>
            </div>

            <div class="card">
                <span class="label">本地节点 (点击复制)</span>
                <div class="copy-box">
                    <span id="node-url">http://127.0.0.1:{{PORT}}</span>
                    <div class="copy-btn" onclick="copyText('node-url')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                    </div>
                </div>
                <span class="label">API KEY</span>
                <div class="copy-box">
                    <span id="api-key">{{API_KEY}}</span>
                    <div class="copy-btn" onclick="copyText('api-key')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                    </div>
                </div>
            </div>

            <div class="card" style="flex:1; overflow-y:auto;">
                <span class="label">历史画廊</span>
                <div class="gallery" id="historyGallery"></div>
            </div>

            <div class="card">
                <span class="label">外观主题</span>
                <div class="theme-selector">
                    <div class="theme-dot active" style="background:#FFBF00;" onclick="changeTheme('obsidian', this)"></div>
                    <div class="theme-dot" style="background:#007AFF;" onclick="changeTheme('deepsea', this)"></div>
                    <div class="theme-dot" style="background:#BF00FF;" onclick="changeTheme('cyber', this)"></div>
                </div>
            </div>
        </div>

        <!-- 主视图 -->
        <div class="main-view">
            <div class="card" style="margin-bottom:20px;">
                <span class="label">任务配置</span>
                <div style="display:flex; gap:10px;">
                    <select id="modelSelect" style="flex:1;">
                        <option value="nano_banana">Nano Banana (极速)</option>
                        <option value="standard">Standard (高清)</option>
                    </select>
                    <select id="ratioSelect" style="flex:1;">
                        <option value="match_input_image">原始比例</option>
                        <option value="1:1">1:1 正方</option>
                        <option value="3:2">3:2 横向</option>
                        <option value="2:3">2:3 纵向</option>
                        <option value="9:16">9:16 竖屏</option>
                        <option value="16:9">16:9 宽屏</option>
                        <option value="3:4">3:4 纵向</option>
                        <option value="4:3">4:3 横向</option>
                    </select>
                </div>
                
                <div class="upload-zone" id="dropZone">
                    <div id="uploadPrompt">拖拽、点击或粘贴参考图</div>
                    <img id="preview-img">
                    <input type="file" id="fileInput" hidden accept="image/*">
                </div>

                <textarea id="promptInput" rows="2" placeholder="输入提示词..."></textarea>
                <button class="btn-action" id="genBtn">执行渲染任务</button>
            </div>

            <div class="terminal-container">
                <div class="terminal-header">
                    <span>CORE TERMINAL</span>
                    <span id="statusText">READY</span>
                </div>
                <div class="terminal-body" id="terminalOut">
                    <div style="color:#444;">> 系统内核已就绪，等待指令...</div>
                </div>
            </div>
        </div>
    </div>

    <div id="toast">已复制到剪贴板</div>

    <script>
        let currentBase64 = null;

        // 复制功能
        function copyText(id) {
            const text = document.getElementById(id).innerText;
            const el = document.createElement('textarea');
            el.value = text;
            document.body.appendChild(el);
            el.select();
            document.execCommand('copy');
            document.body.removeChild(el);
            
            const toast = document.getElementById('toast');
            toast.style.display = 'block';
            setTimeout(() => toast.style.display = 'none', 2000);
        }

        // 主题切换
        function changeTheme(theme, el) {
            document.body.setAttribute('data-theme', theme);
            document.querySelectorAll('.theme-dot').forEach(d => d.classList.remove('active'));
            el.classList.add('active');
            fetch('/api/theme', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({theme}) });
        }

        // 上传逻辑
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const previewImg = document.getElementById('preview-img');
        const uploadPrompt = document.getElementById('uploadPrompt');

        dropZone.onclick = () => fileInput.click();
        const processFile = (file) => {
            if (!file || !file.type.startsWith('image/')) return;
            const reader = new FileReader();
            reader.onload = (e) => {
                currentBase64 = e.target.result;
                previewImg.src = currentBase64;
                previewImg.style.display = 'block';
                uploadPrompt.style.display = 'none';
            };
            reader.readAsDataURL(file);
        };
        fileInput.onchange = (e) => processFile(e.target.files[0]);
        window.addEventListener('paste', (e) => {
            const items = e.clipboardData.items;
            for (let item of items) {
                if (item.type.indexOf('image') !== -1) processFile(item.getAsFile());
            }
        });

        // 数据刷新
        const refreshData = async () => {
            const res = await fetch('/api/data');
            const data = await res.json();
            document.getElementById('stat-total').innerText = data.stats.total_calls;
            document.getElementById('stat-success').innerText = data.stats.success_calls;
            document.getElementById('stat-last').innerText = data.stats.last_call_time;
            
            const gallery = document.getElementById('historyGallery');
            gallery.innerHTML = data.history.map(item => `
                <div class="gallery-item" onclick="window.open('${item.url}')">
                    <img src="${item.url}" title="${item.prompt}">
                </div>
            `).join('');

            if(data.settings.theme) {
                document.body.setAttribute('data-theme', data.settings.theme);
                document.querySelectorAll('.theme-dot').forEach(d => {
                    if(d.getAttribute('onclick').includes(data.settings.theme)) d.classList.add('active');
                    else d.classList.remove('active');
                });
            }
        };
        refreshData();

        // 加载可用模型
        const loadModels = async () => {
            try {
                const res = await fetch('/v1/models');
                const data = await res.json();
                const modelSelect = document.getElementById('modelSelect');
                
                // 清空现有选项（保留第一个作为默认）
                modelSelect.innerHTML = '';
                
                // 添加模型选项
                data.data.forEach(model => {
                    const option = document.createElement('option');
                    option.value = model.id;
                    
                    // 使用API返回的显示名称，如果没有则使用模型ID
                    option.text = model.display_name || model.id;
                    
                    // 标记支持图像的模型
                    if (model.supports_images) {
                        option.text += ' 📷';
                    }
                    
                    modelSelect.appendChild(option);
                });
                
                // 如果没有模型，添加默认选项
                if (data.data.length === 0) {
                    const option = document.createElement('option');
                    option.value = 'nano_banana';
                    option.text = 'Nano Banana (极速)';
                    modelSelect.appendChild(option);
                }
                
                console.log('已加载模型列表:', data.data.length, '个模型');
            } catch (error) {
                console.error('加载模型失败:', error);
                // 保留默认选项
            }
        };
        
        // 页面加载时获取模型列表
        loadModels();

        // 日志输出 - Cherry Studio风格
        const addLog = (tag, msg) => {
            const out = document.getElementById('terminalOut');
            const card = document.createElement('div');
            card.className = `log-card ${tag.toLowerCase()}`;
            
            const content = document.createElement('div');
            content.className = 'log-content';
            content.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
            
            const actions = document.createElement('div');
            actions.className = 'log-actions';
            
            const detailLink = document.createElement('span');
            detailLink.className = 'log-detail';
            detailLink.textContent = '详情';
            detailLink.onclick = () => {
                // 可以展开/折叠详细信息，这里暂时只是复制消息
                navigator.clipboard.writeText(msg).then(() => {
                    const toast = document.getElementById('toast');
                    toast.textContent = '已复制消息到剪贴板';
                    toast.style.display = 'block';
                    setTimeout(() => toast.style.display = 'none', 2000);
                });
            };
            
            const closeBtn = document.createElement('span');
            closeBtn.className = 'log-close';
            closeBtn.textContent = '×';
            closeBtn.onclick = () => card.remove();
            
            actions.appendChild(detailLink);
            actions.appendChild(closeBtn);
            card.appendChild(content);
            card.appendChild(actions);
            out.appendChild(card);
            out.scrollTop = out.scrollHeight;
        };

        // 任务提交
        document.getElementById('genBtn').onclick = async () => {
            const prompt = document.getElementById('promptInput').value;
            if (!prompt) return alert("请输入提示词！");

            const btn = document.getElementById('genBtn');
            const status = document.getElementById('statusText');
            const out = document.getElementById('terminalOut');

            btn.disabled = true;
            out.innerHTML = '';
            status.innerText = 'BUSY';

            try {
                const res = await fetch('/v1/chat/completions', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        model: document.getElementById('modelSelect').value,
                        aspect_ratio: document.getElementById('ratioSelect').value,
                        messages: [{
                            role: 'user',
                            content: [
                                { type: 'text', text: prompt },
                                { type: 'image_url', image_url: { url: currentBase64 } }
                            ]
                        }]
                    })
                });

                const reader = res.body.getReader();
                const decoder = new TextDecoder();

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const chunk = decoder.decode(value);
                    const lines = chunk.split('\\n');
                    
                    for (let line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.substring(6));
                                // 处理调试消息和结果
                                if (data.choices && data.choices[0].delta.content) {
                                    const content = data.choices[0].delta.content;
                                    // 检查是否是调试消息（以 >>> 开头）
                                    if (content.startsWith('>>>')) {
                                        addLog('DEBUG', content);
                                    } else {
                                        // 尝试提取图片URL
                                        const urlMatch = content.match(/\\((.*?)\\)/);
                                        if (urlMatch) {
                                            const url = urlMatch[1];
                                            out.innerHTML += '<div style="margin-top:15px;text-align:center;"><img src="' + url + '" style="max-width:100%;border-radius:10px;border:1px solid var(--primary);"></div>';
                                            status.innerText = 'DONE';
                                            refreshData();
                                        } else {
                                            // 其他文本内容
                                            addLog('INFO', content);
                                        }
                                    }
                                }
                                if (data.error) {
                                    addLog('ERROR', data.error.message);
                                    throw new Error(data.error.message);
                                }
                            } catch(e) {}
                        }
                    }
                }
            } catch (e) {
                addLog('ERROR', e.message);
                status.innerText = 'FAIL';
            } finally {
                btn.disabled = false;
            }
        };
    </script>
</body>
</html>
"""
    content = html_template.replace("{{PROJECT_NAME}}", CONFIG["PROJECT_NAME"])
    content = content.replace("{{PORT}}", str(CONFIG["PORT"]))
    content = content.replace("{{API_KEY}}", CONFIG["API_KEY"])
    content = content.replace("{{VERSION}}", CONFIG["VERSION"])
    return content

# =================================================================
# 启动入口 (Desktop App Entry)
# =================================================================
class Api:
    def __init__(self):
        self.maximized = False

    def close(self):
        window.destroy()
    def minimize(self):
        window.minimize()
    def maximize(self):
        window.maximize()
        self.maximized = True
    def restore(self):
        window.restore()
        self.maximized = False
    def toggle_maximize(self):
        if self.maximized:
            self.restore()
        else:
            self.maximize()

def run_flask():
    app.run(port=CONFIG["PORT"], threaded=True)

if __name__ == "__main__":
    # 1. 启动 Flask
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    # 2. 创建原生窗口 (无边框模式)
    api = Api()
    window = webview.create_window(
        CONFIG["PROJECT_NAME"], 
        f"http://127.0.0.1:{CONFIG['PORT']}",
        width=1280,
        height=820,
        frameless=True,  # 开启无边框模式，实现原生高级感
        easy_drag=True,
        resizable=True,  # 允许调整窗口大小
        min_size=(800, 600),  # 最小窗口尺寸
        background_color='#0F0F12',
        js_api=api
    )
    
    # 3. 启动
    webview.start()