FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories and set permissions
RUN mkdir -p /app/data && \
    chmod +x /app/bot.py

# Create a non-root user
RUN useradd -m -r botuser && \
    chown -R botuser:botuser /app
USER botuser

# Expose port (for Render.com)
EXPOSE 10000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:10000/health || exit 1

# Start the bot
CMD ["python", "bot.py"]
