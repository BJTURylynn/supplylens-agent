# Windows 快速启动

解压源码包后，在 `supplylens-agent` 文件夹内打开 PowerShell。

若已经安装 uv：

```powershell
uv sync --frozen --extra dev --python 3.11
uv run supplylens init
uv run supplylens index
uv run supplylens serve
```

保持第一个终端运行，另开一个 PowerShell，进入同一个文件夹：

```powershell
uv run streamlit run ui/app.py
```

访问 [http://localhost:8501](http://localhost:8501)。先选 `PO-1001`，点击“分析到货情况”，再输入“帮我写一份供应商跟进建议”。

默认模式完全不需要模型密钥；`.env.example` 是配置模板。希望体验真实工具调用时，再按主 README 配置 Cloud 模式。

若没有 uv，也可以使用已安装的 Python 3.11 创建虚拟环境并按锁文件安装：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe -m supplylens.cli init
.venv\Scripts\python.exe -m supplylens.cli index
.venv\Scripts\python.exe -m supplylens.cli serve
```

另一个终端运行：`.venv\Scripts\python.exe -m streamlit run ui/app.py`。

在 Windows 脚本中需要稳定输出中文时，可先设置 `$env:PYTHONIOENCODING = 'utf-8'`。启动问题首先检查 `/health`，并参阅主 README 和验证记录。
