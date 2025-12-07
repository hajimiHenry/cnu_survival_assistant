"""
Web GUI 后端服务
基于 FastAPI 实现 RESTful API
"""
import os
import sys
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

from src.vector_store import load_vector_store
from src.rag_chain import answer_question
from src.state import ConversationState, load_user_state, save_user_state
from src.intent_router import detect_intent


# .env 文件路径
ENV_FILE = project_root / ".env"

# 在模块加载时就加载 .env
load_dotenv(ENV_FILE, override=True)


# ============ 数据模型 ============

class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    intent: str
    new_memories: List[str]


class ProfileData(BaseModel):
    university: str
    college: str
    major: str
    grade: str


class ProfileUpdate(BaseModel):
    college: Optional[str] = None
    major: Optional[str] = None
    grade: Optional[str] = None


class MemoryCreate(BaseModel):
    content: str


class MemoryItem(BaseModel):
    index: int
    content: str
    created_at: str
    source: str


class MessageItem(BaseModel):
    role: str
    content: str
    timestamp: str
    intent: str


class ApiConfig(BaseModel):
    api_key: str
    base_url: str
    model_name: str


# ============ 全局状态 ============

vector_store = None
state = None


# ============ 生命周期管理 ============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global vector_store, state

    print("正在加载知识库...")
    try:
        vector_store = load_vector_store()
        print("知识库加载完成！")
    except FileNotFoundError:
        print("警告: 向量索引不存在，请先运行 build_index.py")
        vector_store = None
    except Exception as e:
        print(f"警告: 加载向量索引失败 - {e}")
        vector_store = None

    print("正在加载用户数据...")
    state = load_user_state()
    print("用户数据加载完成！")

    print("\n" + "=" * 50)
    print("服务已启动！请在浏览器中访问: http://localhost:8080")
    print("=" * 50 + "\n")

    yield

    # 关闭时保存数据
    print("\n正在保存用户数据...")
    save_user_state(state)
    print("数据已保存，服务已关闭。")


# ============ FastAPI 应用 ============

app = FastAPI(
    title="CNU Survival Assistant",
    description="首都师范大学在校生生存助手 Web API",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件目录
static_dir = project_root / "static"
static_dir.mkdir(exist_ok=True)


# ============ 路由 ============

@app.get("/")
async def index():
    """返回前端页面"""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        status_code=404,
        content={"error": "前端页面不存在，请检查 static/index.html"}
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """发送问题，获取回答"""
    global state

    if vector_store is None:
        raise HTTPException(
            status_code=503,
            detail="知识库未加载，请先运行 build_index.py 构建索引"
        )

    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    # 意图识别
    intent, confidence = detect_intent(question)

    try:
        # 获取回答
        answer, new_memories = answer_question(vector_store, question, state)

        # 保存状态
        save_user_state(state)

        return ChatResponse(
            answer=answer,
            intent=intent.value,
            new_memories=new_memories
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理问题时出错: {str(e)}")


@app.get("/api/profile")
async def get_profile():
    """获取用户信息"""
    return {
        "university": state.profile.university,
        "college": state.profile.college,
        "major": state.profile.major,
        "grade": state.profile.grade
    }


@app.put("/api/profile")
async def update_profile(update: ProfileUpdate):
    """更新用户信息"""
    if update.college is not None:
        state.profile.college = update.college
    if update.major is not None:
        state.profile.major = update.major
    if update.grade is not None:
        state.profile.grade = update.grade

    save_user_state(state)

    return {
        "success": True,
        "profile": {
            "university": state.profile.university,
            "college": state.profile.college,
            "major": state.profile.major,
            "grade": state.profile.grade
        }
    }


@app.get("/api/memories")
async def get_memories():
    """获取记忆列表"""
    memories = []
    for i, mem in enumerate(state.memories, 1):
        memories.append({
            "index": i,
            "content": mem.content,
            "created_at": mem.created_at.strftime("%Y-%m-%d %H:%M"),
            "source": mem.source
        })
    return {"memories": memories}


@app.post("/api/memories")
async def add_memory(memory: MemoryCreate):
    """添加记忆"""
    content = memory.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")

    success = state.add_memory(content, source="用户手动添加")
    if success:
        save_user_state(state)
        return {"success": True, "message": "记忆添加成功"}
    else:
        return {"success": False, "message": "该记忆已存在"}


@app.delete("/api/memories/{index}")
async def delete_memory(index: int):
    """删除指定记忆"""
    success = state.remove_memory(index)
    if success:
        save_user_state(state)
        return {"success": True, "message": f"已删除第 {index} 条记忆"}
    else:
        raise HTTPException(status_code=404, detail=f"记忆 {index} 不存在")


@app.delete("/api/memories")
async def clear_memories():
    """清空所有记忆"""
    state.clear_memories()
    save_user_state(state)
    return {"success": True, "message": "所有记忆已清空"}


@app.get("/api/history")
async def get_history():
    """获取对话历史"""
    history = []
    for msg in state.history:
        history.append({
            "role": msg.role,
            "content": msg.content,
            "timestamp": msg.timestamp.strftime("%Y-%m-%d %H:%M"),
            "intent": msg.intent
        })
    return {"history": history}


@app.delete("/api/history")
async def clear_history():
    """清空对话历史"""
    state.clear_history()
    save_user_state(state)
    return {"success": True, "message": "对话历史已清空"}


# ============ API 配置接口 ============

@app.get("/api/config")
async def get_api_config():
    """获取当前 API 配置"""
    # 重新加载 .env 获取最新值
    load_dotenv(ENV_FILE, override=True)

    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "")
    model_name = os.getenv("MODEL_NAME", "")

    # 调试：打印环境变量值（去掉敏感信息）
    print(f"[DEBUG] ENV_FILE: {ENV_FILE}, exists: {ENV_FILE.exists()}")
    print(f"[DEBUG] base_url: {base_url}, model_name: {model_name}, api_key_set: {bool(api_key)}")

    # 隐藏部分 API Key
    masked_key = ""
    if api_key:
        if len(api_key) > 8:
            masked_key = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
        else:
            masked_key = "*" * len(api_key)

    return {
        "api_key": masked_key,
        "api_key_set": bool(api_key),
        "base_url": base_url,
        "model_name": model_name
    }


@app.put("/api/config")
async def update_api_config(config: ApiConfig):
    """更新 API 配置"""
    try:
        # 确保 .env 文件存在
        if not ENV_FILE.exists():
            ENV_FILE.touch()

        # 写入配置
        if config.api_key and not config.api_key.startswith("*"):
            # 只有当 API Key 不是掩码时才更新
            set_key(str(ENV_FILE), "OPENAI_API_KEY", config.api_key)

        if config.base_url:
            set_key(str(ENV_FILE), "OPENAI_BASE_URL", config.base_url)

        if config.model_name:
            set_key(str(ENV_FILE), "MODEL_NAME", config.model_name)

        # 重新加载环境变量
        load_dotenv(ENV_FILE, override=True)

        # 更新运行时环境变量
        if config.api_key and not config.api_key.startswith("*"):
            os.environ["OPENAI_API_KEY"] = config.api_key
        if config.base_url:
            os.environ["OPENAI_BASE_URL"] = config.base_url
        if config.model_name:
            os.environ["MODEL_NAME"] = config.model_name

        return {"success": True, "message": "API 配置已更新，新配置将在下次对话时生效"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存配置失败: {str(e)}")


@app.post("/api/config/test")
async def test_api_config():
    """测试当前 API 配置是否有效"""
    from src.rag_chain import get_llm

    try:
        llm = get_llm()
        # 发送一个简单的测试请求
        response = llm.invoke("你好，请回复'API连接成功'")
        return {
            "success": True,
            "message": "API 连接测试成功！",
            "response": response.content[:100] if hasattr(response, 'content') else str(response)[:100]
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"API 连接测试失败: {str(e)}"
        }


# ============ 运行入口 ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
