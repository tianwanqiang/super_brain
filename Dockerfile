FROM python:3.13-slim

WORKDIR /app

# 默认用腾讯云自己的 PyPI 镜像——服务器部署在腾讯云上，同网络内直连比走境外源快得多。
# 真要在别的地方构建这个镜像，可以用 --build-arg PIP_INDEX_URL=https://pypi.org/simple
# 覆盖回官方源。
# 2026-09-03 起 RAG 改用阿里云云端服务（DashScope + DashVector），不再需要在本地/服务器
# 装 torch 这类机器学习依赖——这里不再有之前"no space left on device"那类问题的土壤。
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt gunicorn

COPY . .

# DEEPSEEK_CONFIG_PATH 不用在这里单独设——paths.py 现在默认就是 SUPER_BRAIN / "config.json"，
# 跟着 SUPER_BRAIN_DIR 自动解析成 /app/config.json，两个环境变量各设一份容易以后改一个
# 忘了改另一个、悄悄读到不同的文件。
ENV SUPER_BRAIN_DIR=/app \
    OPC_ROOT_DIR=/app \
    SUPER_BRAIN_HOST=0.0.0.0 \
    SUPER_BRAIN_PORT=5151

EXPOSE 5151

# -w 1：只用一个 worker。每日 18 点批量汇总的调度线程在模块导入时就启动（不是在
# `if __name__ == "__main__"` 里），多个 worker 会导致每个进程各自起一个调度线程，
# 18 点那一刻会被重复触发多次（重复花 DeepSeek 额度）。这是单用户内部工具，1 个 worker
# 完全够用，不需要为并发能力牺牲这个正确性。
# --timeout 240：机制2定期复盘那个路由是同步阻塞调用 DeepSeek 的（不是后台线程、没有
# SSE 保活机制），gunicorn 默认 30 秒超时可能会把这类请求杀掉。2026-09-04 真实发生过
# --timeout 120 把圆桌讨论的 SSE 连接所在的 worker 杀掉、讨论内容当场从 UI 消失的事故——
# 那次的根因已经在 ui_app.py 的 /roundtable/stream 路由里用短轮询保活字节修掉了（不再
# 依赖单纯拉长这个数字），但"定期复盘"这条路由没有保活机制、纯靠这个数字兜底，所以
# 仍然放宽到 240 秒，给一次真实 DeepSeek 调用（含较长的思考阶段）留够余量。
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5151", "--timeout", "240", "ui_app:app"]
