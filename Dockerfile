# uv vem de uma imagem separada da do Python, ambas travadas por digest. A tag
# combinada "python3.13-bookworm-slim" da própria uv trava, sem aviso, um uv
# desatualizado (0.9.30 no digest original deste plano) que não sabe instalar o
# Python 3.13.14 exigido por .python-version — corrigido durante a execução
# depois de `uv sync` falhar com "No download found for request:
# cpython-3.13.14-linux-x86_64-gnu". Buscar o binário do uv à parte, num estágio
# de build, desacopla as duas versões.
FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependências antes do código: o cache de camada sobrevive a mudança de fonte.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
