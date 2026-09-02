FROM python:3.13-slim

WORKDIR /app

# 默认用腾讯云自己的 PyPI 镜像——服务器部署在腾讯云上，同网络内直连比走境外源快得多
# （RAG 依赖 torch/transformers 这些包体积大，走境外源经常慢到超时）。真要在别的地方
# 构建这个镜像，可以用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖回官方源。
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt gunicorn

COPY . .

ENV SUPER_BRAIN_DIR=/app \
    OPC_ROOT_DIR=/app \
    DEEPSEEK_CONFIG_PATH=/app/config.json \
    SUPER_BRAIN_HOST=0.0.0.0 \
    SUPER_BRAIN_PORT=5151

EXPOSE 5151

# -w 1：只用一个 worker。每日 18 点批量汇总的调度线程在模块导入时就启动（不是在
# `if __name__ == "__main__"` 里），多个 worker 会导致每个进程各自起一个调度线程，
# 18 点那一刻会被重复触发多次（重复花 DeepSeek 额度）。这是单用户内部工具，1 个 worker
# 完全够用，不需要为并发能力牺牲这个正确性。
# --timeout 120：机制2定期复盘那个路由是同步阻塞调用 DeepSeek 的（不是后台线程），
# gunicorn 默认 30 秒超时可能会把这类请求杀掉，放宽到 120 秒。
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5151", "--timeout", "120", "ui_app:app"]
