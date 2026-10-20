# uv 最佳实践文档

> **适用场景**：Python 工具脚本、本地大模型部署（Ollama/Qwen）、API 服务开发  
> **核心工具**：uv（Astral 出品，Rust 编写）  
> **归档编号**：Python-uv-BestPractice-001

---

## 一、uv 定位与核心优势

uv 不是 pip 的替代品，而是 **pip + venv + pip-tools + Poetry 核心功能的统一体**。

| 功能         | 传统方案               | uv 方案                      | 速度对比           |
| ------------ | ---------------------- | ---------------------------- | ------------------ |
| 安装依赖     | `pip install`          | `uv pip install`             | **10~50 倍**       |
| 创建虚拟环境 | `python -m venv`       | `uv venv`                    | **80 倍**          |
| 解析依赖图   | `pip` 或 `poetry lock` | `uv lock` / `uv pip compile` | **秒级 vs 分钟级** |
| 运行脚本     | 手动激活 venv          | `uv run python`              | 零配置，自动激活   |
| 全局工具安装 | `pipx`                 | `uv tool install`            | 内置，无需额外工具 |

**关键设计哲学**：
- **兼容优先**：完全兼容 `requirements.txt` 和 `pyproject.toml`
- **可复现构建**：`uv.lock` 锁定文件确保团队环境完全一致
- **无全局污染**：所有项目自带隔离环境，告别 `sudo pip install`

---

## 二、安装与 Shell 配置

### 2.1 安装（跨平台）
```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 验证
uv --version  # 预期：0.5.x 或更高
```

### 2.2 国内镜像配置（必做）
uv 默认从 PyPI 拉取，国内需配置镜像：

```bash
# 全局配置（推荐）
uv pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 或仅对当前项目（写入 pyproject.toml）
uv add --index-url https://pypi.tuna.tsinghua.edu.cn/simple requests
```

**常用镜像地址**：
- 清华：`https://pypi.tuna.tsinghua.edu.cn/simple`
- 阿里云：`https://mirrors.aliyun.com/pypi/simple/`

### 2.3 Shell 自动补全
```bash
# Bash
echo 'eval "$(uv generate-shell-completion bash)"' >> ~/.bashrc

# Zsh
echo 'eval "$(uv generate-shell-completion zsh)"' >> ~/.zshrc

# 重新加载
source ~/.bashrc  # 或 ~/.zshrc
```

---

## 三、项目初始化标准流程

### 3.1 从零创建项目
```bash
# 创建项目目录并初始化
uv init neuramind-ai-tools
cd neuramind-ai-tools

# 目录结构
# .
# ├── .python-version      # 锁定 Python 版本
# ├── pyproject.toml       # 项目配置 + 依赖声明
# ├── README.md
# └── src/
#     └── neuramind_ai_tools/
#         └── __init__.py
```

**`pyproject.toml` 初始内容**：
```toml
[project]
name = "neuramind-ai-tools"
version = "0.1.0"
description = "AI tools for NeuraMind"
readme = "README.md"
requires-python = ">=3.11"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### 3.2 指定 Python 版本（大模型项目建议 3.11+）
```bash
# 安装特定 Python 版本（uv 会自动下载管理）
uv python install 3.11

# 为当前项目固定版本
uv python pin 3.11
# 生成 .python-version 文件，团队成员进入目录自动识别
```

### 3.3 推荐目录结构（工具脚本型项目）
```text
neuramind-ai-tools/
├── .python-version
├── pyproject.toml
├── uv.lock                   # 依赖锁定文件（必须提交 Git）
├── README.md
├── src/
│   └── neuramind_ai_tools/
│       ├── __init__.py
│       ├── ollama_client.py    # Ollama 本地调用封装
│       └── qwen_finetune.py    # Qwen 微调脚本
├── scripts/
│   └── download_model.py       # 模型下载辅助脚本
└── tests/
    └── test_client.py
```

---

## 四、依赖管理最佳实践

### 4.1 添加依赖
```bash
# 生产依赖（写入 pyproject.toml dependencies）
uv add requests httpx ollama

# 开发依赖（仅本地开发/测试使用）
uv add --dev pytest ruff black

# 指定版本
uv add torch>=2.2.0
uv add "numpy<2.0"  # 复杂约束需引号包裹

# 指定 Git 仓库
uv add git+https://github.com/user/repo.git
```

### 4.2 移除与升级
```bash
# 移除依赖（自动清理 pyproject.toml 和 uv.lock）
uv remove requests

# 升级单个包
uv lock --upgrade-package numpy

# 升级所有包
uv lock --upgrade
```

### 4.3 锁定文件（uv.lock）—— 核心机制
```bash
# 根据 pyproject.toml 生成精确锁定文件
uv lock

# 同步环境到锁定状态（团队协作后必做）
uv sync
```

**uv.lock 规则**：
- **必须提交到 Git**，它是构建可复现的基石
- 包含每个包的精确版本、哈希校验、来源 URL
- 与 Go 的 `go.sum`、Node 的 `package-lock.json` 地位等同

### 4.4 依赖类型区分
| 命令                      | 作用                     | 写入位置                        |
| ------------------------- | ------------------------ | ------------------------------- |
| `uv add <pkg>`            | 生产依赖                 | `project.dependencies`          |
| `uv add --dev <pkg>`      | 开发/测试依赖            | `dependency-groups.dev`         |
| `uv add --optional <pkg>` | 可选依赖（如 CUDA 支持） | `project.optional-dependencies` |

**pyproject.toml 中的体现**：
```toml
[project]
dependencies = [
    "requests>=2.31.0",
    "ollama>=0.1.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "ruff>=0.3.0",
]

[project.optional-dependencies]
cuda = [
    "torch>=2.2.0",
]
```

---

## 五、虚拟环境管理

### 5.1 创建与激活（传统方式）
```bash
uv venv                    # 创建 .venv
source .venv/bin/activate  # Linux/macOS 激活
.venv\Scripts\activate     # Windows 激活
```

### 5.2 推荐方式：uv run（零激活）
```bash
# 自动检测 .venv 并激活，无需手动 source
uv run python src/ollama_client.py

# 运行模块
uv run python -m pytest

# 运行脚本并传递参数
uv run python scripts/download_model.py --model qwen2.5-coder:7b

# 直接运行工具（无需全局安装）
uv run ruff check .
uv run black src/
```

**优势**：告别 `source .venv/bin/activate` 的记忆负担，CI 脚本也更简洁。

### 5.3 全局工具安装（替代 pipx）
```bash
# 安装全局 CLI 工具（隔离在各自环境，不污染系统 Python）
uv tool install ruff       # 代码检查
uv tool install black      # 格式化
uv tool install httpie     # HTTP 调试
uv tool install jupyterlab # Jupyter

# 使用
ruff check .
httpie GET http://localhost:11434/api/tags

# 升级/卸载
uv tool upgrade ruff
uv tool uninstall black
```

---

## 六、大模型/AI 项目依赖策略

### 6.1 PyTorch 生态（Qwen/Ollama 相关）
PyTorch 包体积大、有 CPU/GPU 版本区分，需显式指定索引：

```bash
# CPU 版本（开发机无显卡时）
uv add torch --index-url https://download.pytorch.org/whl/cpu

# CUDA 12.1 版本（生产环境有 NVIDIA 显卡）
uv add torch --index-url https://download.pytorch.org/whl/cu121

# 或在 pyproject.toml 中配置
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true  # 仅用于显式指定的包，不全局覆盖
```

### 6.2 大模型依赖组合（典型）
```bash
# 核心 AI 栈
uv add ollama openai transformers accelerate

# 数据处理
uv add datasets numpy pandas

# 可选：vLLM 推理加速（仅 Linux + CUDA）
uv add vllm --optional

# 开发辅助
uv add --dev jupyterlab matplotlib
```

### 6.3 模型文件管理（与 uv 无关但重要）
大模型权重文件（GB 级）**不应**放入 Git 或 Python 包：
```python
# src/neuramind_ai_tools/config.py
import os

MODEL_DIR = os.path.expanduser("~/.cache/neuramind/models")
OLLAMA_BASE_URL = os.getenv("OLLAMA_HOST", "http://localhost:11434")
```

---

## 七、与旧项目迁移

### 7.1 从 requirements.txt 迁移
```bash
# 进入旧项目
cd old-project

# 初始化 uv 项目结构
uv init

# 导入现有依赖
uv add -r requirements.txt

# 生成锁定文件
uv lock

# 删除旧文件（确认 uv.lock 生成后）
rm requirements.txt
```

### 7.2 遗留项目兼容模式（仅外部项目过渡）
```bash
# 针对尚未迁移到 pyproject.toml 的外部遗留项目，可临时使用 uv pip 子命令
uv pip install -r requirements.txt      # 安装
uv pip freeze > requirements.txt        # 导出
uv pip compile requirements.in -o requirements.txt  # 解析依赖树
```

### 7.3 从 Poetry 迁移
```bash
# 读取 poetry.lock 中的版本约束，重新生成 uv.lock
# 手动复制 poetry 的 dependencies 到 pyproject.toml
# 然后：
uv lock
```

---

## 八、CI/CD 与 Docker 集成

### 8.1 GitHub Actions 缓存
```yaml
- name: Setup uv
  uses: astral-sh/setup-uv@v3
  with:
    version: "0.5.0"

- name: Cache dependencies
  uses: actions/cache@v4
  with:
    path: |
      ~/.cache/uv
      .venv
    key: ${{ runner.os }}-uv-${{ hashFiles('uv.lock') }}

- name: Install dependencies
  run: uv sync --frozen  # 严格按 uv.lock 安装，不更新

- name: Run tests
  run: uv run pytest
```

### 8.2 Docker 多阶段构建（极致体积）
```dockerfile
# 阶段一：依赖层（利用 uv 的极速安装）
FROM ghcr.io/astral-sh/uv:0.5-python3.11-bookworm-slim AS deps
WORKDIR /app
COPY uv.lock pyproject.toml ./
RUN uv sync --frozen --no-dev  # 仅生产依赖

# 阶段二：运行
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY src/ ./src/
CMD ["python", "-m", "neuramind_ai_tools"]
```

### 8.3 构建检查清单
```bash
# CI 中必须执行的检查
uv sync --frozen          # 验证 uv.lock 与 pyproject.toml 同步
uv run pytest             # 测试
uv run ruff check .       # 代码风格
uv run mypy src/          # 类型检查（如使用）
```

---

## 九、常用工具链集成

### 9.1 Ruff（格式化 + 检查，替代 Black + Flake8）
```bash
uv add --dev ruff

# pyproject.toml 配置
[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I"]  # 错误、未定义、导入排序
```

### 9.2 Jupyter Notebook
```bash
uv add --dev jupyterlab
uv run jupyter lab
```

### 9.3 类型检查（可选）
```bash
uv add --dev mypy
uv run mypy src/
```

---

## 十、常见问题排查手册

| 现象                                 | 根因                        | 解决                                                         |
| ------------------------------------ | --------------------------- | ------------------------------------------------------------ |
| `uv.lock` 与 `pyproject.toml` 不同步 | 手动编辑了 toml 未重新 lock | 执行 `uv lock`                                               |
| `uv sync` 后代码 import 报错         | 未在虚拟环境中运行          | 使用 `uv run python` 而非系统 python                         |
| `torch` 安装后无法识别 CUDA          | 安装了 CPU 版本             | 指定 `--index-url https://download.pytorch.org/whl/cu121`    |
| 下载速度极慢                         | 未配置国内镜像              | `uv pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| `uv add` 报版本冲突                  | 依赖树存在不兼容约束        | `uv tree` 查看冲突，手动指定兼容版本                         |
| Windows 下 `uv run` 找不到脚本       | PowerShell 执行策略限制     | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned`          |
| 全局 `ruff` 命令不存在               | 未用 `uv tool install` 安装 | `uv tool install ruff` 或改用 `uv run ruff`                  |

---

## 十一、命令速查表

```bash
# 项目初始化
uv init <name>              # 创建新项目
uv python install 3.11      # 安装特定 Python 版本
uv python pin 3.11          # 固定项目 Python 版本

# 依赖管理
uv add <pkg>                # 添加生产依赖
uv add --dev <pkg>          # 添加开发依赖
uv remove <pkg>             # 移除依赖
uv lock                     # 生成/更新 uv.lock
uv sync                     # 同步环境到 uv.lock
uv sync --frozen            # 严格同步，不更新 lock
uv sync --no-dev            # 仅同步生产依赖

# 虚拟环境与运行
uv venv                     # 创建虚拟环境
uv run <command>            # 在虚拟环境中运行命令（推荐）
uv run python <script.py>   # 运行脚本

# 查看与调试
uv tree                     # 查看依赖树
uv pip list                 # 已安装包列表
uv pip show <pkg>           # 包详情

# 全局工具
uv tool install <pkg>       # 安装全局 CLI 工具
uv tool upgrade <pkg>       # 升级全局工具
uv tool uninstall <pkg>     # 卸载
uv tool list                # 查看已安装工具

# 构建与导出
uv build                    # 构建 wheel/sdist
uv pip freeze               # 导出 requirements.txt（兼容模式）
```

---

## 十二、团队提交规范

**提交前自检：**
1. `uv lock` 后 `uv sync --frozen` 能正常通过
2. `uv run pytest` 测试全部通过
3. `uv run ruff check .` 无格式错误
4. `uv.lock` 已加入 Git 提交

**Code Review 关注点：**
- `pyproject.toml` 中是否出现无约束的版本号（如 `torch` 而非 `torch>=2.2.0`）？
- 是否误将 GB 级模型文件或数据集路径提交到仓库？
- AI 相关依赖是否区分了 CPU/CUDA 版本并在文档中说明？

---

> **归档建议**：本文档配合 `.python-version` 固定版本、`uv.lock` 锁定依赖，可确保你的 Ollama/Qwen 本地环境在重装系统或团队协作时一键复现。与之前的 Go Modules、Gradle Kotlin DSL 文档共同构成你的全栈工程化笔记体系。