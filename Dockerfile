FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: the MCP server (the long-running service this image is meant to deploy,
# e.g. on Render -- see README.md's "Render deployment" section). It uses stdio
# unless MCP_TRANSPORT=streamable-http is set in the environment, in which case it
# binds 0.0.0.0:$PORT and requires MCP_AUTH_TOKEN.
#
# To run a one-shot pipeline command instead, override the container command, e.g.:
#   docker run --env-file .env cos-sales-agent python main.py --mode=demo
CMD ["python", "-m", "app.mcp.server"]
