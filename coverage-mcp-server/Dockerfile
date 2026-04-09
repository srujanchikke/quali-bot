FROM python:3.13-slim

WORKDIR /app

# Copy both pyproject.toml and source before installing (hatchling needs source)
COPY pyproject.toml .
COPY src/ src/

# Install the package
RUN pip install --no-cache-dir .

# Run as non-root
RUN useradd -r -s /bin/false appuser
USER appuser

# Coverage files are mounted here at runtime:
#   docker run -v /path/to/coverage:/data -e COVERAGE_FILE_PATH=/data/index.json ...
# Set COVERAGE_DIR=/data or COVERAGE_FILE_PATH=/data/index.json in env
VOLUME ["/data"]

EXPOSE 8080

CMD ["coverage-mcp-server"]
