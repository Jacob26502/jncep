FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY jncep ./jncep
RUN pip install --no-cache-dir .

# config + tracking file in /config/jncep (click app dir follows XDG on Linux)
ENV XDG_CONFIG_HOME=/config
VOLUME ["/config", "/books"]

EXPOSE 8765

ENTRYPOINT ["jncep", "webhook"]
