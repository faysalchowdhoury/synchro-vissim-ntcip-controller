FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MPLBACKEND=Agg
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
COPY examples ./examples
RUN pip install --no-deps . && useradd --create-home --uid 10001 app
USER app
# 8000/tcp progression API, 1161/udp NTCIP 1202 agent (non-privileged SNMP port)
EXPOSE 8000 1161/udp
CMD ["uvicorn", "ntcip_ctrl.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
